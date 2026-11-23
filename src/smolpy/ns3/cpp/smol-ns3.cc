// smol-ns3: the program that runs SMOLPy scenarios on ns-3 (ADR-0004).
//
// It is built once per ns-3 installation by smolpy.ns3, and each run reads a
// scenario that smolpy.ns3 has lowered to plain lines of integers, with every
// time already in integer nanoseconds (Model Semantics §1):
//
//     smol-ns3 3
//     duration <ns>
//     tcp <min RTO ns> <clock granularity ns> <delayed ACK ns> <SYN RTO ns>
//     adapter <mac> <ipv4>                   nodes, numbered from 0 in order
//     switch <buffer B>                     0: unbounded egress queues
//     link <a> <b> <speed num> <speed den> <propagation ns> <gap ns> <strict a> <strict b>
//     static <switch> <mac> <port>          a switch's MAC table at the start
//     flow <src> <dst ipv4> <udp payload> <priority> <delay ns> <period num> <period den>
//     sends <src> <dst ipv4> <priority> <n> <t ns> <udp payload> ...
//     sink <node>                           a node that receives flows
//     connect <client> <server> <port>      a TCP connection, numbered from 0
//     poll <connection> <request B> <response B> <delay ns> <period num> <period den> <outstanding> <timeout ns>
//     publish <connection> <message B> <qos> <topic> <delay ns> <period num> <period den>
//     route <broker> <topic> <connection>...  the subscribers' connections
//     ar <controller> <device> <ar> <cycle ns> <hold> <delay ns> <output frame B> <input frame B>
//     observe <kind> <target> <period num> <period den> <device>...
//
// A flow is a constant one of fixed-size frames; "sends" lists every frame of
// any other flow, its instant and size drawn by smolpy.ns3 from the flow's
// random stream exactly as the reference engine draws them. Modbus and MQTT
// messages are sized in bytes of TCP payload, and topics are numbered. A
// poll's outstanding is 0 for unlimited or 1 (ADR-0012), and its timeout 0
// for none. The tcp line sets ns-3's TCP to Linux's where that shapes
// recovery (ADR-0012), before any socket exists.
// PROFINET RT frames are layer 2 and sized as whole frames, 802.1Q tag and
// padding included.
//
// Link i has the devices 2i (on a) and 2i + 1 (on b); a node's ports are
// numbered in the order of its links. Speeds are exact rationals in Mb/s and
// periods in ns.
//
// The output has one line per sample, "<observation> <t ns> <a> <b>", with
// raw integer counters; smolpy.ns3 computes each metric from them with the
// reference engine's formulas, so that every value is rounded once, as there.
//
// What ns-3 contributes is its IPv4 stack, with UDP for flows and TCP for
// Modbus and MQTT; the link and the switch are SMOL's own, to give the same
// Ethernet as the other engines:
// - SmolDevice: a full-duplex Ethernet port (ns-3's CSMA is half duplex).
//   A frame is its IP packet plus header and FCS (18 B), at least 64 B, and
//   an 802.1Q tag (4 B) with a priority above 0; it occupies the wire for
//   preamble, SFD and frame, and the next frame waits for the 12 B gap
//   (ADR-0010). The egress queue serves priorities strictly, FIFO within
//   one, or is plain FIFO on a switch with qos=fifo. On a switch with a
//   buffer it holds at most that many frame bytes, the frame in
//   transmission not counted, and drops a frame that does not fit (tail
//   drop, ADR-0011). An idle port takes a queued frame after the events
//   already scheduled for that instant, as the reference engine does, so
//   frames queued at one instant compete by priority and are admitted
//   against the same buffer.
// - SmolSwitch: store-and-forward, learning, flooding unknown destinations to
//   every port but the one a frame came in on, dropping hairpins.
// - Neighbour caches start full: SMOL models no ARP.

#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <deque>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace smol
{

using namespace ns3;
using u128 = unsigned __int128;

constexpr uint64_t ETHERNET_BYTES = 14 + 4; // header and FCS
constexpr uint64_t VLAN_TAG_BYTES = 4;
constexpr uint64_t MIN_FRAME_BYTES = 64;
constexpr uint64_t PREAMBLE_SFD_BYTES = 8;
constexpr uint64_t WIRE_OVERHEAD_BITS = (8 + 12) * 8; // preamble, SFD and gap

[[noreturn]] void
Fail(const std::string& what)
{
    std::cerr << "smol-ns3: " << what << std::endl;
    std::exit(2);
}

// round(num / den), ties to even (timebase._round_ratio)
uint64_t
RoundRatio(u128 num, u128 den)
{
    u128 q = num / den;
    u128 r = num % den;
    u128 twice = 2 * r;
    if (twice > den || (twice == den && (q & 1)))
    {
        ++q;
    }
    return static_cast<uint64_t>(q);
}

// Exact rationals (speeds, periods) can have numerators and denominators
// beyond 64 bits: a rate of 405.2631578947368 frames/s is a period of
// 5 * 10^21 / 2055921052631579 ns. They are read as unsigned 128-bit integers.
std::istream&
operator>>(std::istream& in, u128& value)
{
    std::string digits;
    if (!(in >> digits))
    {
        return in;
    }
    u128 v = 0;
    for (char c : digits)
    {
        if (c < '0' || c > '9' || v > (~u128(0) - 9) / 10)
        {
            in.setstate(std::ios::failbit); // not a number, or beyond 128 bits
            return in;
        }
        v = v * 10 + static_cast<unsigned>(c - '0');
    }
    value = v;
    return in;
}

// The instants start + round(k * num / den) for k = 1, 2, ... (timebase.instants)
class Instants
{
  public:
    Instants(uint64_t start = 0, u128 num = 1, u128 den = 1)
        : m_start(start),
          m_num(num),
          m_den(den)
    {
    }

    uint64_t Next()
    {
        m_total += m_num;
        return m_start + (m_den == 1 ? static_cast<uint64_t>(m_total) : RoundRatio(m_total, m_den));
    }

  private:
    uint64_t m_start;
    u128 m_num;
    u128 m_den;
    u128 m_total = 0;
};

uint64_t
MacKey(const Mac48Address& mac)
{
    uint8_t b[6];
    mac.CopyTo(b);
    uint64_t key = 0;
    for (uint8_t byte : b)
    {
        key = (key << 8) | byte;
    }
    return key;
}

// The time a flow's data was created, carried to the receiving port.
class OriginTag : public Tag
{
  public:
    OriginTag(uint64_t created = 0)
        : m_created(created)
    {
    }

    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("smol::OriginTag").SetParent<Tag>().AddConstructor<OriginTag>();
        return tid;
    }

    TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }

    uint32_t GetSerializedSize() const override
    {
        return 8;
    }

    void Serialize(TagBuffer i) const override
    {
        i.WriteU64(m_created);
    }

    void Deserialize(TagBuffer i) override
    {
        m_created = i.ReadU64();
    }

    void Print(std::ostream& os) const override
    {
        os << "created=" << m_created;
    }

    uint64_t Created() const
    {
        return m_created;
    }

  private:
    uint64_t m_created;
};

// The 802.1Q priority (PCP) of a flow's frames, above 0.
class PriorityTag : public Tag
{
  public:
    PriorityTag(uint8_t priority = 0)
        : m_priority(priority)
    {
    }

    static TypeId GetTypeId()
    {
        static TypeId tid =
            TypeId("smol::PriorityTag").SetParent<Tag>().AddConstructor<PriorityTag>();
        return tid;
    }

    TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }

    uint32_t GetSerializedSize() const override
    {
        return 1;
    }

    void Serialize(TagBuffer i) const override
    {
        i.WriteU8(m_priority);
    }

    void Deserialize(TagBuffer i) override
    {
        m_priority = i.ReadU8();
    }

    void Print(std::ostream& os) const override
    {
        os << "priority=" << int(m_priority);
    }

    uint8_t Priority() const
    {
        return m_priority;
    }

  private:
    uint8_t m_priority;
};

constexpr uint16_t PN_ETHERTYPE = 0x8892;
constexpr uint8_t PN_RT_PRIORITY = 6;

struct Frame
{
    Ptr<Packet> packet;
    Mac48Address src;
    Mac48Address dst;
    uint16_t protocol = 0;
    uint64_t bytes = 0;
    uint8_t priority = 0;
    int32_t ar = -1; // a PROFINET RT frame's AR
};

struct Window
{
    uint64_t sum = 0;
    uint64_t count = 0;
    uint64_t max = 0; // pn_jitter
};

// The consumer of PROFINET RT cyclic data on one node, across its ARs, as
// smolpy.sim.engine._PnConsumer (Model Semantics §7).
class PnConsumer
{
  public:
    std::vector<Window*> latencyWindows;
    std::vector<Window*> jitterWindows;
    uint64_t expiries = 0;

    void Expect(int32_t ar, uint64_t cycleNs, uint64_t hold)
    {
        Ar& a = m_ars[ar];
        a.cycleNs = cycleNs;
        a.timeoutNs = hold * cycleNs;
    }

    void Record(int32_t id, uint64_t origin)
    {
        auto it = m_ars.find(id);
        if (it == m_ars.end())
        {
            return;
        }
        Ar& ar = it->second;
        uint64_t now = Simulator::Now().GetNanoSeconds();
        for (Window* w : latencyWindows)
        {
            w->sum += now - origin;
            ++w->count;
        }
        if (ar.started)
        {
            int64_t delta = static_cast<int64_t>(now - ar.last) - static_cast<int64_t>(ar.cycleNs);
            uint64_t jitter = static_cast<uint64_t>(delta < 0 ? -delta : delta);
            for (Window* w : jitterWindows)
            {
                w->max = std::max(w->max, jitter);
                ++w->count;
            }
        }
        // Supervision starts with the first frame and restarts with each
        // one. A frame that arrives exactly at the deadline is too late: its
        // arrival was scheduled after the deadline, so the deadline comes
        // first, as on SimPy, where the watchdog's timeout fires before the
        // frame is delivered.
        ar.started = true;
        ar.last = now;
        ar.watchdog.Cancel();
        ar.watchdog = Simulator::Schedule(NanoSeconds(ar.timeoutNs), &PnConsumer::Expire, this);
    }

  private:
    struct Ar
    {
        uint64_t cycleNs = 0;
        uint64_t timeoutNs = 0; // hold x cycle
        bool started = false;
        uint64_t last = 0;
        EventId watchdog;
    };

    // Counted once per gap; the exchange goes on (ADR-0005).
    void Expire()
    {
        ++expiries;
    }

    std::unordered_map<int32_t, Ar> m_ars;
};

class SmolChannel;
class SmolSwitch;

class SmolDevice : public NetDevice
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("smol::SmolDevice")
                                .SetParent<NetDevice>()
                                .SetGroupName("Smol")
                                .AddConstructor<SmolDevice>();
        return tid;
    }

    void Configure(u128 speedNum,
                   u128 speedDen,
                   uint64_t gapNs,
                   bool strict,
                   uint64_t buffer,
                   Ptr<SmolChannel> channel)
    {
        m_speedNum = speedNum;
        m_speedDen = speedDen;
        m_gapNs = gapNs;
        m_strict = strict;
        m_buffer = buffer;
        m_channel = channel;
    }

    void SetSwitch(SmolSwitch* sw, uint32_t port)
    {
        m_switch = sw;
        m_port = port;
    }

    // A frame at the end of its propagation (SmolChannel).
    void Receive(Frame frame);

    // A frame for the egress queue. A bounded queue drops it if its bytes
    // would exceed the buffer. An idle port takes it at this instant, after
    // the events already scheduled for it (Model Semantics §3, §9).
    void Enqueue(Frame frame)
    {
        if (m_buffer > 0 && m_queuedBytes + frame.bytes > m_buffer)
        {
            ++dropped; // tail drop: nothing is queued or scheduled
            return;
        }
        m_queuedBytes += frame.bytes;
        m_queue[m_strict ? frame.priority : 0].push_back(std::move(frame));
        ++m_queued;
        if (!m_busy && !m_starting)
        {
            m_starting = true;
            Simulator::ScheduleNow(&SmolDevice::Start, this);
        }
    }

    uint64_t QueueLength() const
    {
        return m_queued; // the frame on the wire is not queued
    }

    uint64_t bitsSent = 0;       // frame bits whose transmission has completed
    uint64_t framesSent = 0;     // those frames
    uint64_t bytesReceived = 0;  // frames addressed to this port
    uint64_t framesReceived = 0; // those frames
    uint64_t dropped = 0;        // frames a bounded queue dropped
    std::vector<Window*> latencyWindows;
    PnConsumer* pn = nullptr; // the node's PROFINET consumer

    // NetDevice
    void SetIfIndex(const uint32_t index) override
    {
        m_ifIndex = index;
    }

    uint32_t GetIfIndex() const override
    {
        return m_ifIndex;
    }

    Ptr<Channel> GetChannel() const override;

    void SetAddress(Address address) override
    {
        m_address = Mac48Address::ConvertFrom(address);
    }

    Address GetAddress() const override
    {
        return m_address;
    }

    bool SetMtu(const uint16_t mtu) override
    {
        m_mtu = mtu;
        return true;
    }

    uint16_t GetMtu() const override
    {
        return m_mtu;
    }

    bool IsLinkUp() const override
    {
        return true;
    }

    void AddLinkChangeCallback(Callback<void>) override
    {
    }

    bool IsBroadcast() const override
    {
        return true;
    }

    Address GetBroadcast() const override
    {
        return Mac48Address::GetBroadcast();
    }

    bool IsMulticast() const override
    {
        return true;
    }

    Address GetMulticast(Ipv4Address group) const override
    {
        return Mac48Address::GetMulticast(group);
    }

    Address GetMulticast(Ipv6Address addr) const override
    {
        return Mac48Address::GetMulticast(addr);
    }

    bool IsPointToPoint() const override
    {
        return false;
    }

    bool IsBridge() const override
    {
        return false;
    }

    bool Send(Ptr<Packet> packet, const Address& dest, uint16_t protocol) override
    {
        return SendFrom(packet, m_address, dest, protocol);
    }

    bool SendFrom(Ptr<Packet> packet,
                  const Address& source,
                  const Address& dest,
                  uint16_t protocol) override
    {
        Frame frame;
        PriorityTag tag;
        if (packet->PeekPacketTag(tag))
        {
            frame.priority = tag.Priority();
        }
        frame.bytes = std::max(MIN_FRAME_BYTES, packet->GetSize() + ETHERNET_BYTES) +
                      (frame.priority > 0 ? VLAN_TAG_BYTES : 0);
        frame.packet = packet;
        frame.src = Mac48Address::ConvertFrom(source);
        frame.dst = Mac48Address::ConvertFrom(dest);
        frame.protocol = protocol;
        Enqueue(std::move(frame));
        return true;
    }

    Ptr<Node> GetNode() const override
    {
        return m_node;
    }

    void SetNode(Ptr<Node> node) override
    {
        m_node = node;
    }

    bool NeedsArp() const override
    {
        return true;
    }

    void SetReceiveCallback(NetDevice::ReceiveCallback cb) override
    {
        m_rx = cb;
    }

    void SetPromiscReceiveCallback(PromiscReceiveCallback) override
    {
    }

    bool SupportsSendFrom() const override
    {
        return true;
    }

  protected:
    void DoDispose() override
    {
        m_channel = nullptr;
        m_node = nullptr;
        m_rx = MakeNullCallback<bool, Ptr<NetDevice>, Ptr<const Packet>, uint16_t, const Address&>();
        for (auto& q : m_queue)
        {
            q.clear();
        }
        m_sending = Frame();
        NetDevice::DoDispose();
    }

  private:
    uint64_t TransmissionNs(uint64_t bytes)
    {
        auto it = m_txNs.find(bytes);
        if (it != m_txNs.end())
        {
            return it->second;
        }
        // (preamble, SFD and frame) bits / (speed Mb/s) in ns: bits * 1000 * den / num
        u128 bits = static_cast<u128>(PREAMBLE_SFD_BYTES + bytes) * 8;
        uint64_t ns = RoundRatio(bits * 1000 * m_speedDen, m_speedNum);
        m_txNs.emplace(bytes, ns);
        return ns;
    }

    void Start()
    {
        m_starting = false;
        if (!m_busy && m_queued > 0)
        {
            StartNext();
        }
    }

    void StartNext()
    {
        m_busy = true;
        // the highest priority first, FIFO within one
        auto q = std::find_if(m_queue.rbegin(), m_queue.rend(), [](const auto& c) {
            return !c.empty();
        });
        m_sending = std::move(q->front());
        q->pop_front();
        --m_queued;
        m_queuedBytes -= m_sending.bytes; // in transmission: out of the buffer
        uint64_t now = Simulator::Now().GetNanoSeconds();
        uint64_t gap = m_freeAt > now ? m_freeAt - now : 0; // the gap after the last frame
        Simulator::Schedule(NanoSeconds(gap + TransmissionNs(m_sending.bytes)),
                            &SmolDevice::TransmitComplete,
                            this);
    }

    void TransmitComplete();

    Ptr<SmolChannel> m_channel;
    Ptr<Node> m_node;
    SmolSwitch* m_switch = nullptr;
    uint32_t m_port = 0;
    NetDevice::ReceiveCallback m_rx;
    Mac48Address m_address;
    uint32_t m_ifIndex = 0;
    uint16_t m_mtu = 65535; // no IP fragmentation: SMOL frames may be jumbo
    u128 m_speedNum = 1;
    u128 m_speedDen = 1;
    uint64_t m_gapNs = 0;
    bool m_strict = true;
    uint64_t m_buffer = 0; // bytes; 0: unbounded
    std::array<std::deque<Frame>, 8> m_queue; // by priority; all in 0 if not strict
    uint64_t m_queued = 0;
    uint64_t m_queuedBytes = 0;
    Frame m_sending;
    bool m_busy = false;
    bool m_starting = false; // a Start is scheduled for this instant
    uint64_t m_freeAt = 0; // the end of the gap after the last frame
    std::unordered_map<uint64_t, uint64_t> m_txNs;
};

NS_OBJECT_ENSURE_REGISTERED(SmolDevice);

// One full-duplex link: each port transmits to the other.
class SmolChannel : public Channel
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("smol::SmolChannel")
                                .SetParent<Channel>()
                                .SetGroupName("Smol")
                                .AddConstructor<SmolChannel>();
        return tid;
    }

    void Connect(Ptr<SmolDevice> a, Ptr<SmolDevice> b, uint64_t propagationNs)
    {
        m_ends[0] = a;
        m_ends[1] = b;
        m_propagationNs = propagationNs;
    }

    void Transmit(const SmolDevice* from, Frame frame)
    {
        SmolDevice* to = PeekPointer(m_ends[from == PeekPointer(m_ends[0]) ? 1 : 0]);
        Simulator::Schedule(NanoSeconds(m_propagationNs), &SmolDevice::Receive, to, frame);
    }

    std::size_t GetNDevices() const override
    {
        return 2;
    }

    Ptr<NetDevice> GetDevice(std::size_t i) const override
    {
        return m_ends[i];
    }

  protected:
    void DoDispose() override
    {
        m_ends[0] = nullptr;
        m_ends[1] = nullptr;
        Channel::DoDispose();
    }

  private:
    Ptr<SmolDevice> m_ends[2];
    uint64_t m_propagationNs = 0;
};

NS_OBJECT_ENSURE_REGISTERED(SmolChannel);

Ptr<Channel>
SmolDevice::GetChannel() const
{
    return m_channel;
}

void
SmolDevice::TransmitComplete()
{
    uint64_t now = Simulator::Now().GetNanoSeconds();
    m_freeAt = now + m_gapNs;
    bitsSent += m_sending.bytes * 8;
    ++framesSent;
    m_channel->Transmit(this, std::move(m_sending));
    m_sending = Frame();
    m_busy = false;
    if (m_queued > 0)
    {
        StartNext();
    }
}

// Store-and-forward, learning switch, as smolpy.sim.engine._switch_forwarder.
class SmolSwitch
{
  public:
    std::vector<Ptr<SmolDevice>> ports; // in the order of the switch's links
    std::unordered_map<uint64_t, uint32_t> table;

    void Receive(uint32_t in, Frame frame)
    {
        table[MacKey(frame.src)] = in;
        auto it = table.find(MacKey(frame.dst));
        if (it == table.end())
        {
            // unknown destination: a copy to every port but the one it came in on
            for (uint32_t p = 0; p < ports.size(); ++p)
            {
                if (p != in)
                {
                    Frame copy = frame;
                    copy.packet = frame.packet->Copy();
                    ports[p]->Enqueue(std::move(copy));
                }
            }
        }
        else if (it->second != in)
        {
            ports[it->second]->Enqueue(std::move(frame));
        }
        // the port it came in on: a hairpin, dropped
    }

    uint64_t QueueDepth() const
    {
        uint64_t depth = 0;
        for (const auto& port : ports)
        {
            depth += port->QueueLength();
        }
        return depth;
    }

    uint64_t Dropped() const
    {
        uint64_t n = 0;
        for (const auto& port : ports)
        {
            n += port->dropped;
        }
        return n;
    }

    uint64_t buffer = 0; // bytes per port; 0: unbounded
};

void
SmolDevice::Receive(Frame frame)
{
    if (m_switch)
    {
        m_switch->Receive(m_port, std::move(frame));
        return;
    }
    // a NIC takes only frames for its own MAC: flooded ones count nowhere
    if (frame.dst != m_address)
    {
        return;
    }
    bytesReceived += frame.bytes;
    ++framesReceived;
    // a UDP datagram's or PROFINET frame's origin is a packet tag; TCP data
    // carries it as a byte tag, the first one in a segment; segments without
    // data have none
    OriginTag tag;
    if (frame.packet->PeekPacketTag(tag) || frame.packet->FindFirstMatchingByteTag(tag))
    {
        uint64_t latency = Simulator::Now().GetNanoSeconds() - tag.Created();
        for (Window* w : latencyWindows)
        {
            w->sum += latency;
            ++w->count;
        }
    }
    if (frame.protocol == PN_ETHERTYPE)
    {
        if (pn) // PROFINET RT is layer 2: not for IP
        {
            pn->Record(frame.ar, tag.Created());
        }
        return;
    }
    m_rx(this, frame.packet, frame.protocol, frame.src);
}

// A constant flow of UDP datagrams, as smolpy.sim.engine._traffic_gen.
class SmolFlow : public Application
{
  public:
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("smol::SmolFlow").SetParent<Application>().AddConstructor<SmolFlow>();
        return tid;
    }

    // a constant flow of fixed-size frames
    void Configure(Ipv4Address dst,
                   uint8_t priority,
                   uint32_t payload,
                   Instants instants,
                   uint64_t stopNs)
    {
        m_dst = dst;
        m_priority = priority;
        m_payload = payload;
        m_instants = instants;
        m_stopNs = stopNs;
    }

    // any other flow: every frame, its instant and UDP payload
    void Configure(Ipv4Address dst,
                   uint8_t priority,
                   std::vector<std::pair<uint64_t, uint32_t>> sends)
    {
        m_dst = dst;
        m_priority = priority;
        m_sends = std::move(sends);
        m_listed = true;
    }

  private:
    void StartApplication() override
    {
        m_socket = Socket::CreateSocket(GetNode(), UdpSocketFactory::GetTypeId());
        m_socket->Bind();
        m_socket->Connect(InetSocketAddress(m_dst, 9));
        ScheduleNext();
    }

    void StopApplication() override
    {
    }

    void ScheduleNext()
    {
        uint64_t t;
        if (m_listed)
        {
            if (m_next == m_sends.size())
            {
                return;
            }
            t = m_sends[m_next].first;
        }
        else
        {
            t = m_instants.Next();
            if (t >= m_stopNs)
            {
                return;
            }
        }
        Simulator::Schedule(NanoSeconds(t - Simulator::Now().GetNanoSeconds()),
                            &SmolFlow::Send,
                            this);
    }

    void SendOne(uint32_t payload)
    {
        Ptr<Packet> packet = Create<Packet>(payload);
        packet->AddPacketTag(OriginTag(Simulator::Now().GetNanoSeconds()));
        if (m_priority > 0)
        {
            packet->AddPacketTag(PriorityTag(m_priority));
        }
        m_socket->Send(packet);
    }

    void Send()
    {
        if (m_listed)
        {
            // a batch: every frame of this instant, back to back
            uint64_t now = Simulator::Now().GetNanoSeconds();
            while (m_next < m_sends.size() && m_sends[m_next].first == now)
            {
                SendOne(m_sends[m_next++].second);
            }
        }
        else
        {
            SendOne(m_payload);
        }
        ScheduleNext();
    }

    Ptr<Socket> m_socket;
    Ipv4Address m_dst;
    uint8_t m_priority = 0;
    uint32_t m_payload = 0;
    Instants m_instants;
    uint64_t m_stopNs = 0;
    bool m_listed = false;
    std::vector<std::pair<uint64_t, uint32_t>> m_sends;
    std::size_t m_next = 0;
};

NS_OBJECT_ENSURE_REGISTERED(SmolFlow);

// --- Modbus TCP and MQTT over ns-3's TCP ---
//
// Each master-slave or client-broker pair has one persistent TCP connection,
// opened at the start. The applications send whole protocol messages; the
// bytes themselves carry nothing, and each connection keeps, per direction,
// the messages whose bytes are on the way, in order. TCP delivers the bytes
// in order, so a message has arrived when all of its bytes have. Each
// message's bytes carry its origin time as a byte tag, for the latency of
// the frames that carry them.

constexpr uint16_t MODBUS_PORT = 502;
constexpr uint16_t MQTT_PORT = 1883;
constexpr uint32_t MQTT_PUBACK_MESSAGE_BYTES = 4;

enum class Message : uint8_t
{
    ModbusRequest,
    ModbusResponse,
    Publish,
    Forward, // a PUBLISH from a broker to a subscriber
    Puback,
};

struct Transaction;

struct Sent
{
    Message kind;
    uint32_t bytes;
    uint64_t origin;
    uint32_t reply = 0; // a request's response bytes, or a PUBLISH's topic
    uint8_t qos = 0;
    // a poll with one request in flight: its slot, and the MBAP transaction
    // id, which the response carries back
    Transaction* transaction = nullptr;
    uint64_t tid = 0;
};

class Connection;

// The protocol state of a node (an adapter, slave or broker).
struct Host
{
    PnConsumer pn;
    std::vector<Window*> rttWindows;                               // modbus_latency
    std::unordered_map<uint32_t, std::vector<Connection*>> routes; // broker: topic -> subscribers
    uint64_t waiting = 0; // broker: messages waiting for a subscriber's connection
    uint64_t modbusRequests = 0;  // sent by its polls
    uint64_t modbusResponses = 0; // accepted
    uint64_t modbusTimeouts = 0;  // requests timed out (ADR-0012)
    uint64_t modbusSkipped = 0;   // poll instants skipped (ADR-0012)
};

// The transaction slot of a poll with one request in flight (ADR-0012), as
// smolpy.sim.engine._Transaction: the request outstanding, if any, and when
// the previous one completed. It lives in the application, above TCP: a
// request that has timed out may still be in TCP's byte stream.
struct Transaction
{
    Host* master;
    uint64_t timeoutNs; // 0: none
    uint64_t sent = 0;  // requests sent: the last one's id is sent - 1
    uint64_t sentAt = 0;
    bool outstanding = false;
    int64_t completedAt = -1;

    // Whether a request may be sent now: none is outstanding, and the
    // previous one did not complete at this very instant.
    bool Free(uint64_t now) const
    {
        return !outstanding && completedAt != static_cast<int64_t>(now);
    }

    uint64_t Send(uint64_t now)
    {
        sentAt = now;
        outstanding = true;
        return sent++;
    }

    // Whether a response ends the outstanding request: its own, before the
    // timeout. A response at exactly the timeout is late, whichever of the
    // two events ns-3 runs first.
    bool Accept(uint64_t tid, uint64_t now)
    {
        if (!outstanding || tid + 1 != sent || (timeoutNs > 0 && now - sentAt >= timeoutNs))
        {
            return false;
        }
        outstanding = false;
        completedAt = static_cast<int64_t>(now);
        return true;
    }

    // The timeout of request tid: it times out if still outstanding.
    void Expire(uint64_t tid)
    {
        if (outstanding && tid + 1 == sent)
        {
            outstanding = false;
            completedAt = Simulator::Now().GetNanoSeconds();
            ++master->modbusTimeouts;
        }
    }
};

class Connection
{
  public:
    enum End
    {
        Client = 0,
        Server = 1,
    };

    Connection(Ptr<Node> client, Ipv4Address server, uint16_t port, Host* clientHost, Host* serverHost)
        : m_client(client),
          m_server(server),
          m_port(port)
    {
        m_hosts[Client] = clientHost;
        m_hosts[Server] = serverHost;
    }

    void Open()
    {
        Ptr<Socket> s = Socket::CreateSocket(m_client, TcpSocketFactory::GetTypeId());
        s->Bind();
        s->SetRecvCallback(MakeCallback(&Connection::ReceiveAtClient, this));
        s->Connect(InetSocketAddress(m_server, m_port));
        Attach(Client, s); // data may be sent while the handshake runs
    }

    Host* ClientHost() const
    {
        return m_hosts[Client];
    }

    // The server's end, accepted.
    void Accepted(Ptr<Socket> s)
    {
        s->SetRecvCallback(MakeCallback(&Connection::ReceiveAtServer, this));
        Attach(Server, s);
    }

    void Send(End from, Sent message)
    {
        if (!m_sockets[from])
        {
            m_waiting[from].push_back(message);
            ++m_hosts[from]->waiting;
            return;
        }
        Ptr<Packet> packet = Create<Packet>(message.bytes);
        packet->AddByteTag(OriginTag(message.origin));
        if (m_sockets[from]->Send(packet) < 0)
        {
            Fail("a TCP send buffer overflowed");
        }
        m_inFlight[1 - from].push_back(message);
    }

  private:
    void Attach(End end, Ptr<Socket> s)
    {
        m_sockets[end] = s;
        std::deque<Sent> waiting;
        std::swap(waiting, m_waiting[end]);
        m_hosts[end]->waiting -= waiting.size();
        for (const Sent& message : waiting)
        {
            Send(end, message);
        }
    }

    void ReceiveAtClient(Ptr<Socket> s)
    {
        Receive(Client, s);
    }

    void ReceiveAtServer(Ptr<Socket> s)
    {
        Receive(Server, s);
    }

    void Receive(End at, Ptr<Socket> s)
    {
        while (Ptr<Packet> p = s->Recv())
        {
            m_received[at] += p->GetSize();
        }
        std::deque<Sent>& coming = m_inFlight[at];
        while (!coming.empty() && m_received[at] >= coming.front().bytes)
        {
            Sent message = coming.front();
            coming.pop_front();
            m_received[at] -= message.bytes;
            Arrived(at, message);
        }
    }

    // A reply, sent at this instant once TCP has handled the segment that
    // completed the message. TCP sends what one end has received data to
    // send at once, and other data one time step (1 ns) later; going
    // through the event queue, every reply takes the second way, in the
    // order the application makes them, as on the reference engine.
    static void Reply(Connection* c, End from, Sent message)
    {
        Simulator::ScheduleNow(&Connection::Send, c, from, message);
    }

    // A whole message has arrived at one end: the application acts at once.
    void Arrived(End at, const Sent& message)
    {
        uint64_t now = Simulator::Now().GetNanoSeconds();
        Host* host = m_hosts[at];
        switch (message.kind)
        {
        case Message::ModbusRequest:
            // the response carries the request's origin, for the round trip,
            // and its transaction id
            Reply(this,
                  Server,
                  {Message::ModbusResponse,
                   message.reply,
                   message.origin,
                   0,
                   0,
                   message.transaction,
                   message.tid});
            break;
        case Message::ModbusResponse:
            if (message.transaction && !message.transaction->Accept(message.tid, now))
            {
                break; // a late response: the master has given up on it
            }
            ++host->modbusResponses;
            for (Window* w : host->rttWindows)
            {
                w->sum += now - message.origin;
                ++w->count;
            }
            break;
        case Message::Publish:
            if (auto route = host->routes.find(message.reply); route != host->routes.end())
            {
                for (Connection* subscriber : route->second)
                {
                    Reply(subscriber, Server, {Message::Forward, message.bytes, message.origin});
                }
            }
            if (message.qos > 0)
            {
                Reply(this, Server, {Message::Puback, MQTT_PUBACK_MESSAGE_BYTES, now});
            }
            break;
        case Message::Forward:
        case Message::Puback:
            break;
        }
    }

    Ptr<Node> m_client;
    Ipv4Address m_server;
    uint16_t m_port;
    Host* m_hosts[2];
    Ptr<Socket> m_sockets[2];
    std::deque<Sent> m_waiting[2];  // to send from an end whose socket is not there yet
    std::deque<Sent> m_inFlight[2]; // on the way to an end, in order
    uint64_t m_received[2] = {0, 0}; // bytes of the first message there that have arrived
};

// The listening socket of a slave or broker, which hands each accepted
// connection to its Connection by the client's address.
class Listener
{
  public:
    Listener(Ptr<Node> node, uint16_t port)
    {
        m_socket = Socket::CreateSocket(node, TcpSocketFactory::GetTypeId());
        m_socket->Bind(InetSocketAddress(Ipv4Address::GetAny(), port));
        m_socket->Listen();
        m_socket->SetAcceptCallback(MakeNullCallback<bool, Ptr<Socket>, const Address&>(),
                                    MakeCallback(&Listener::Accept, this));
    }

    void Expect(Ipv4Address client, Connection* connection)
    {
        m_connections[client.Get()] = connection;
    }

  private:
    void Accept(Ptr<Socket> s, const Address& from)
    {
        uint32_t client = InetSocketAddress::ConvertFrom(from).GetIpv4().Get();
        auto it = m_connections.find(client);
        if (it != m_connections.end())
        {
            it->second->Accepted(s);
        }
    }

    Ptr<Socket> m_socket;
    std::unordered_map<uint32_t, Connection*> m_connections;
};

// A PROFINET RT provider: one cyclic frame each cycle from its start, as
// smolpy.sim.engine._pn_provider. It is layer 2, handed to the device.
struct PnProvider
{
    Ptr<SmolDevice> device;
    Mac48Address dst;
    int32_t ar;
    uint64_t bytes;
    uint64_t cycleNs;
    uint64_t stopNs;

    void Fire()
    {
        uint64_t now = Simulator::Now().GetNanoSeconds();
        Frame frame;
        frame.packet = Create<Packet>();
        frame.packet->AddPacketTag(OriginTag(now));
        frame.src = Mac48Address::ConvertFrom(device->GetAddress());
        frame.dst = dst;
        frame.protocol = PN_ETHERTYPE;
        frame.bytes = bytes; // tagged and padded by the model's formula
        frame.priority = PN_RT_PRIORITY;
        frame.ar = ar;
        device->Enqueue(std::move(frame));
        if (now + cycleNs < stopNs)
        {
            Simulator::Schedule(NanoSeconds(cycleNs), &PnProvider::Fire, this);
        }
    }
};

// Modbus polls or MQTT PUBLISH messages at constant instants, as
// smolpy.sim.engine._modbus_poll_gen and _mqtt_traffic_gen. A poll with one
// request in flight hands a request to its connection only when its slot is
// free, and skips the instant otherwise.
struct Periodic
{
    Connection* connection;
    Sent message;
    Instants instants;
    uint64_t stopNs;
    Transaction* transaction = nullptr;

    void Next()
    {
        uint64_t t = instants.Next();
        if (t < stopNs)
        {
            Simulator::Schedule(NanoSeconds(t - Simulator::Now().GetNanoSeconds()),
                                &Periodic::Fire,
                                this);
        }
    }

    void Fire()
    {
        uint64_t now = Simulator::Now().GetNanoSeconds();
        Sent m = message;
        m.origin = now;
        if (transaction)
        {
            if (!transaction->Free(now))
            {
                ++connection->ClientHost()->modbusSkipped;
                Next();
                return;
            }
            m.transaction = transaction;
            m.tid = transaction->Send(now);
        }
        if (m.kind == Message::ModbusRequest)
        {
            ++connection->ClientHost()->modbusRequests;
        }
        connection->Send(Connection::Client, m);
        if (transaction && transaction->timeoutNs > 0 && transaction->timeoutNs < stopNs - now)
        {
            Simulator::Schedule(NanoSeconds(transaction->timeoutNs),
                                &Transaction::Expire,
                                transaction,
                                m.tid);
        }
        Next();
    }
};

enum class Kind
{
    Throughput,
    Latency,
    BytesSent,
    BytesReceived,
    QueueDepth,
    Utilization,
    ModbusLatency,
    BrokerQueue,
    PnLatency,
    PnJitter,
    PnWatchdogExpiries,
    Drops, // drops and drops_total: the total so far
    FramesSent,
    FramesReceived,
    ModbusUnanswered,
    ModbusTimeouts,
    ModbusSkipped,
};

struct Observation
{
    uint32_t index;
    Kind kind;
    Ptr<SmolDevice> device;          // an adapter's port
    SmolSwitch* sw = nullptr;        // queue_depth, drops
    std::vector<Ptr<SmolDevice>> in; // utilization: the ports that send to the target
    Host* host = nullptr;            // broker_queue
    Window window;                   // latency, modbus_latency
    Instants ticks;
    uint64_t stopNs;
    std::ostream* out;
};

void
Sample(Observation* o)
{
    uint64_t now = Simulator::Now().GetNanoSeconds();
    uint64_t a = 0;
    uint64_t b = 0;
    switch (o->kind)
    {
    case Kind::Throughput:
    case Kind::BytesReceived:
        a = o->device->bytesReceived;
        break;
    case Kind::Latency:
    case Kind::ModbusLatency:
    case Kind::PnLatency:
        a = o->window.sum;
        b = o->window.count;
        o->window = Window();
        break;
    case Kind::BytesSent:
        a = o->device->bitsSent;
        break;
    case Kind::QueueDepth:
        a = o->sw->QueueDepth();
        break;
    case Kind::Utilization:
        for (const auto& d : o->in)
        {
            a += d->bitsSent + d->framesSent * WIRE_OVERHEAD_BITS;
        }
        break;
    case Kind::BrokerQueue:
        a = o->host->waiting;
        break;
    case Kind::PnJitter:
        a = o->window.max;
        b = o->window.count;
        o->window = Window();
        break;
    case Kind::PnWatchdogExpiries:
        a = o->host->pn.expiries;
        break;
    case Kind::Drops:
        a = o->sw->Dropped();
        break;
    case Kind::FramesSent:
        a = o->device->framesSent;
        break;
    case Kind::FramesReceived:
        a = o->device->framesReceived;
        break;
    case Kind::ModbusUnanswered:
        a = o->host->modbusRequests;
        b = o->host->modbusResponses;
        break;
    case Kind::ModbusTimeouts:
        a = o->host->modbusTimeouts;
        break;
    case Kind::ModbusSkipped:
        a = o->host->modbusSkipped;
        break;
    }
    *o->out << o->index << ' ' << now << ' ' << a << ' ' << b << '\n';
    uint64_t next = o->ticks.Next();
    if (next < o->stopNs)
    {
        Simulator::Schedule(NanoSeconds(next - now), &Sample, o);
    }
}

int
Run(std::istream& in, std::ostream& out)
{
    Time::SetResolution(Time::NS);
    // TCP for Modbus and MQTT: Ethernet's MSS, and a 20 B header on every
    // segment, as SMOL sizes their frames (no timestamp option). The
    // applications never block: the send buffer holds what TCP cannot send yet.
    Config::SetDefault("ns3::TcpSocket::SegmentSize", UintegerValue(1460));
    Config::SetDefault("ns3::TcpSocket::SndBufSize", UintegerValue(1U << 30));
    Config::SetDefault("ns3::TcpSocketBase::Timestamp", BooleanValue(false));

    std::string word;
    uint32_t version = 0;
    if (!(in >> word >> version) || word != "smol-ns3" || version != 3)
    {
        Fail("not a smol-ns3 version 3 scenario");
    }

    uint64_t durationNs = 0;
    NodeContainer nodes;
    std::vector<bool> isSwitch;
    std::vector<Mac48Address> macs;
    std::vector<Ipv4Address> ips;
    std::deque<SmolSwitch> switches; // stable: ports point to their switch
    std::vector<int> switchOf;
    std::vector<Ptr<SmolDevice>> devices;
    std::vector<Ptr<SmolDevice>> portOf; // an adapter's port
    std::vector<Observation*> observations;
    InternetStackHelper stack;
    stack.SetIpv6StackInstall(false); // SMOL frames are IPv4 only: no IPv6 chatter
    uint64_t switchMacs = 0;
    bool tcp = false;

    std::string line;
    std::getline(in, line);
    while (std::getline(in, line))
    {
        std::istringstream s(line);
        if (!(s >> word))
        {
            continue;
        }
        if (word == "duration")
        {
            s >> durationNs;
        }
        else if (word == "tcp")
        {
            // ns-3's TCP as Linux's (ADR-0012): RTO = SRTT + max(G, 4 RTTVAR),
            // at least MinRto, is Linux's SRTT + RTTVAR with RTTVAR floored at
            // rto_min when G = MinRto
            uint64_t minRtoNs;
            uint64_t granularityNs;
            uint64_t delAckNs;
            uint64_t synRtoNs;
            s >> minRtoNs >> granularityNs >> delAckNs >> synRtoNs;
            if (!s || nodes.GetN() > 0)
            {
                Fail("bad tcp: " + line);
            }
            Config::SetDefault("ns3::TcpSocketBase::MinRto", TimeValue(NanoSeconds(minRtoNs)));
            Config::SetDefault("ns3::TcpSocketBase::ClockGranularity",
                               TimeValue(NanoSeconds(granularityNs)));
            Config::SetDefault("ns3::TcpSocket::DelAckTimeout", TimeValue(NanoSeconds(delAckNs)));
            Config::SetDefault("ns3::TcpSocket::ConnTimeout", TimeValue(NanoSeconds(synRtoNs)));
            tcp = true;
        }
        else if (word == "adapter" || word == "switch")
        {
            Ptr<Node> node = CreateObject<Node>();
            nodes.Add(node);
            bool sw = word == "switch";
            isSwitch.push_back(sw);
            switchOf.push_back(sw ? static_cast<int>(switches.size()) : -1);
            portOf.push_back(nullptr);
            if (sw)
            {
                switches.emplace_back();
                if (!(s >> switches.back().buffer))
                {
                    Fail("bad switch: " + line);
                }
                macs.emplace_back();
                ips.emplace_back();
            }
            else
            {
                std::string mac;
                std::string ip;
                s >> mac >> ip;
                macs.emplace_back(mac.c_str());
                ips.emplace_back(ip.c_str());
            }
        }
        else if (word == "link")
        {
            uint32_t a;
            uint32_t b;
            u128 speedNum;
            u128 speedDen;
            uint64_t propagationNs;
            uint64_t gapNs;
            int strict[2];
            s >> a >> b >> speedNum >> speedDen >> propagationNs >> gapNs >> strict[0] >> strict[1];
            if (!s || a >= nodes.GetN() || b >= nodes.GetN())
            {
                Fail("bad link: " + line);
            }
            Ptr<SmolChannel> channel = CreateObject<SmolChannel>();
            Ptr<SmolDevice> ends[2];
            uint32_t at[2] = {a, b};
            for (int e = 0; e < 2; ++e)
            {
                Ptr<SmolDevice> d = CreateObject<SmolDevice>();
                // only a switch bounds its egress queues: end stations never drop
                uint64_t buffer = isSwitch[at[e]] ? switches[switchOf[at[e]]].buffer : 0;
                d->Configure(speedNum, speedDen, gapNs, strict[e] != 0, buffer, channel);
                Ptr<Node> node = nodes.Get(at[e]);
                if (isSwitch[at[e]])
                {
                    // switch ports have locally administered MACs of their own
                    uint8_t mac[6] = {0x06, 0x53, 0, 0, 0, 0};
                    uint64_t n = ++switchMacs;
                    for (int i = 5; i >= 2; --i, n >>= 8)
                    {
                        mac[i] = n & 0xff;
                    }
                    Mac48Address address;
                    address.CopyFrom(mac);
                    d->SetAddress(address);
                    SmolSwitch& sw = switches[switchOf[at[e]]];
                    d->SetSwitch(&sw, sw.ports.size());
                    sw.ports.push_back(d);
                }
                else
                {
                    d->SetAddress(macs[at[e]]);
                    portOf[at[e]] = d;
                }
                node->AddDevice(d);
                ends[e] = d;
                devices.push_back(d);
            }
            channel->Connect(ends[0], ends[1], propagationNs);
        }
        else if (word == "static")
        {
            uint32_t sw;
            std::string mac;
            uint32_t port;
            s >> sw >> mac >> port;
            switches[switchOf[sw]].table[MacKey(Mac48Address(mac.c_str()))] = port;
        }
        else if (word == "flow" || word == "sends" || word == "sink" || word == "connect" ||
                 word == "poll" || word == "publish" || word == "route" || word == "ar" ||
                 word == "observe")
        {
            break; // the network is complete: IPv4 and neighbour caches come first
        }
        else
        {
            Fail("unknown line: " + line);
        }
    }
    if (!tcp)
    {
        Fail("no tcp line before the network");
    }

    // IPv4 on every adapter with a link, one /32 address, every destination
    // on the link, and full neighbour caches
    for (uint32_t i = 0; i < nodes.GetN(); ++i)
    {
        if (isSwitch[i] || !portOf[i])
        {
            continue;
        }
        Ptr<Node> node = nodes.Get(i);
        stack.Install(node);
        Ptr<Ipv4> ipv4 = node->GetObject<Ipv4>();
        int32_t ifIndex = ipv4->AddInterface(portOf[i]);
        ipv4->AddAddress(ifIndex, Ipv4InterfaceAddress(ips[i], Ipv4Mask("/32")));
        ipv4->SetUp(ifIndex);
        Ipv4StaticRoutingHelper routing;
        routing.GetStaticRouting(ipv4)->AddNetworkRouteTo(Ipv4Address("0.0.0.0"),
                                                          Ipv4Mask("0.0.0.0"),
                                                          ifIndex);
    }
    for (uint32_t i = 0; i < nodes.GetN(); ++i)
    {
        if (isSwitch[i] || !portOf[i])
        {
            continue;
        }
        Ptr<Ipv4L3Protocol> l3 = nodes.Get(i)->GetObject<Ipv4L3Protocol>();
        Ptr<ArpCache> cache = l3->GetInterface(l3->GetInterfaceForDevice(portOf[i]))->GetArpCache();
        for (uint32_t j = 0; j < nodes.GetN(); ++j)
        {
            if (j != i && !isSwitch[j] && portOf[j])
            {
                ArpCache::Entry* entry = cache->Add(ips[j]);
                entry->SetMacAddress(macs[j]);
                entry->MarkPermanent();
            }
        }
    }

    std::vector<Host> hosts(nodes.GetN());
    for (uint32_t i = 0; i < nodes.GetN(); ++i)
    {
        if (portOf[i])
        {
            portOf[i]->pn = &hosts[i].pn;
        }
    }
    std::vector<std::unique_ptr<PnProvider>> providers;
    std::vector<std::unique_ptr<Connection>> connections;
    std::map<std::pair<uint32_t, uint16_t>, std::unique_ptr<Listener>> listeners;
    std::vector<std::unique_ptr<Periodic>> periodics;
    std::vector<std::unique_ptr<Transaction>> transactions;
    auto connection = [&](std::size_t c) {
        if (c >= connections.size())
        {
            Fail("bad connection: " + line);
        }
        return connections[c].get();
    };

    // traffic and observations: the line that ended the network is first
    do
    {
        std::istringstream s(line);
        if (!(s >> word))
        {
            continue;
        }
        if (word == "connect")
        {
            uint32_t client;
            uint32_t server;
            uint16_t port;
            s >> client >> server >> port;
            if (!s || client >= nodes.GetN() || server >= nodes.GetN() || !portOf[client])
            {
                Fail("bad connect: " + line);
            }
            connections.push_back(std::make_unique<Connection>(nodes.Get(client),
                                                               ips[server],
                                                               port,
                                                               &hosts[client],
                                                               &hosts[server]));
            Connection* c = connections.back().get();
            if (portOf[server]) // a server without a link never answers
            {
                auto& listener = listeners[{server, port}];
                if (!listener)
                {
                    listener = std::make_unique<Listener>(nodes.Get(server), port);
                }
                listener->Expect(ips[client], c);
            }
            Simulator::Schedule(NanoSeconds(0), &Connection::Open, c);
        }
        else if (word == "poll" || word == "publish")
        {
            std::size_t c;
            Sent message{};
            uint32_t qos = 0;
            uint64_t delayNs;
            u128 num;
            u128 den;
            uint32_t outstanding = 0;
            uint64_t timeoutNs = 0;
            if (word == "poll")
            {
                message.kind = Message::ModbusRequest;
                s >> c >> message.bytes >> message.reply >> delayNs >> num >> den >> outstanding >>
                    timeoutNs;
                if (outstanding > 1 || (timeoutNs > 0 && outstanding == 0))
                {
                    Fail("bad poll: " + line);
                }
            }
            else
            {
                message.kind = Message::Publish;
                s >> c >> message.bytes >> qos >> message.reply >> delayNs >> num >> den;
                message.qos = qos;
            }
            if (!s)
            {
                Fail("bad " + word + ": " + line);
            }
            Transaction* transaction = nullptr;
            if (outstanding == 1)
            {
                transactions.push_back(
                    std::make_unique<Transaction>(Transaction{connection(c)->ClientHost(), timeoutNs}));
                transaction = transactions.back().get();
            }
            periodics.push_back(std::make_unique<Periodic>(Periodic{connection(c),
                                                                    message,
                                                                    Instants(delayNs, num, den),
                                                                    durationNs,
                                                                    transaction}));
            periodics.back()->Next();
        }
        else if (word == "ar")
        {
            uint32_t controller;
            uint32_t device;
            int32_t ar;
            uint64_t cycleNs;
            uint64_t hold;
            uint64_t delayNs;
            uint64_t outputs;
            uint64_t inputs;
            s >> controller >> device >> ar >> cycleNs >> hold >> delayNs >> outputs >> inputs;
            if (!s || controller >= nodes.GetN() || device >= nodes.GetN() || !portOf[controller] ||
                !portOf[device] || cycleNs == 0)
            {
                Fail("bad ar: " + line);
            }
            hosts[controller].pn.Expect(ar, cycleNs, hold); // inputs
            hosts[device].pn.Expect(ar, cycleNs, hold);     // outputs
            // the controller's provider first, as on the reference engine
            uint32_t from[2] = {controller, device};
            uint64_t bytes[2] = {outputs, inputs};
            for (int i = 0; i < 2; ++i)
            {
                providers.push_back(std::make_unique<PnProvider>(PnProvider{portOf[from[i]],
                                                                            macs[from[1 - i]],
                                                                            ar,
                                                                            bytes[i],
                                                                            cycleNs,
                                                                            durationNs}));
                if (delayNs < durationNs)
                {
                    Simulator::Schedule(NanoSeconds(delayNs),
                                        &PnProvider::Fire,
                                        providers.back().get());
                }
            }
        }
        else if (word == "route")
        {
            uint32_t broker;
            uint32_t topic;
            s >> broker >> topic;
            if (!s || broker >= nodes.GetN())
            {
                Fail("bad route: " + line);
            }
            std::vector<Connection*>& subscribers = hosts[broker].routes[topic];
            std::size_t c;
            while (s >> c)
            {
                subscribers.push_back(connection(c));
            }
        }
        else if (word == "flow" || word == "sends")
        {
            uint32_t src;
            std::string dst;
            uint32_t priority;
            Ptr<SmolFlow> flow = CreateObject<SmolFlow>();
            if (word == "flow")
            {
                uint32_t payload;
                uint64_t delayNs;
                u128 num;
                u128 den;
                s >> src >> dst >> payload >> priority >> delayNs >> num >> den;
                flow->Configure(Ipv4Address(dst.c_str()),
                                priority,
                                payload,
                                Instants(delayNs, num, den),
                                durationNs);
            }
            else
            {
                std::size_t n;
                s >> src >> dst >> priority >> n;
                std::vector<std::pair<uint64_t, uint32_t>> sends(n);
                for (auto& send : sends)
                {
                    s >> send.first >> send.second;
                }
                flow->Configure(Ipv4Address(dst.c_str()), priority, std::move(sends));
            }
            if (!s || src >= nodes.GetN() || !portOf[src] || priority > 7)
            {
                Fail("bad " + word + ": " + line.substr(0, 200));
            }
            nodes.Get(src)->AddApplication(flow);
            flow->SetStartTime(NanoSeconds(0));
        }
        else if (word == "sink")
        {
            uint32_t n;
            s >> n;
            Ptr<Socket> sink = Socket::CreateSocket(nodes.Get(n), UdpSocketFactory::GetTypeId());
            sink->Bind(InetSocketAddress(Ipv4Address::GetAny(), 9));
            sink->SetRecvCallback(MakeCallback(+[](Ptr<Socket> socket) {
                while (socket->Recv())
                {
                }
            }));
        }
        else if (word == "observe")
        {
            std::string kind;
            uint32_t target;
            u128 num;
            u128 den;
            s >> kind >> target >> num >> den;
            auto* o = new Observation();
            o->index = observations.size();
            o->out = &out;
            o->stopNs = durationNs;
            o->ticks = Instants(0, num, den);
            o->device = portOf[target];
            if (kind == "throughput")
            {
                o->kind = Kind::Throughput;
            }
            else if (kind == "latency")
            {
                o->kind = Kind::Latency;
                o->device->latencyWindows.push_back(&o->window);
            }
            else if (kind == "bytes_sent")
            {
                o->kind = Kind::BytesSent;
            }
            else if (kind == "bytes_received")
            {
                o->kind = Kind::BytesReceived;
            }
            else if (kind == "queue_depth")
            {
                o->kind = Kind::QueueDepth;
                o->sw = &switches[switchOf[target]];
            }
            else if (kind == "utilization")
            {
                o->kind = Kind::Utilization;
                uint32_t d;
                while (s >> d)
                {
                    o->in.push_back(devices.at(d));
                }
            }
            else if (kind == "modbus_latency")
            {
                o->kind = Kind::ModbusLatency;
                hosts.at(target).rttWindows.push_back(&o->window);
            }
            else if (kind == "broker_queue")
            {
                o->kind = Kind::BrokerQueue;
                o->host = &hosts.at(target);
            }
            else if (kind == "pn_latency")
            {
                o->kind = Kind::PnLatency;
                hosts.at(target).pn.latencyWindows.push_back(&o->window);
            }
            else if (kind == "pn_jitter")
            {
                o->kind = Kind::PnJitter;
                hosts.at(target).pn.jitterWindows.push_back(&o->window);
            }
            else if (kind == "pn_watchdog_expiries")
            {
                o->kind = Kind::PnWatchdogExpiries;
                o->host = &hosts.at(target);
            }
            else if (kind == "drops")
            {
                o->kind = Kind::Drops;
                o->sw = &switches[switchOf[target]];
            }
            else if (kind == "frames_sent")
            {
                o->kind = Kind::FramesSent;
            }
            else if (kind == "frames_received")
            {
                o->kind = Kind::FramesReceived;
            }
            else if (kind == "modbus_unanswered")
            {
                o->kind = Kind::ModbusUnanswered;
                o->host = &hosts.at(target);
            }
            else if (kind == "modbus_timeouts")
            {
                o->kind = Kind::ModbusTimeouts;
                o->host = &hosts.at(target);
            }
            else if (kind == "modbus_skipped")
            {
                o->kind = Kind::ModbusSkipped;
                o->host = &hosts.at(target);
            }
            else
            {
                Fail("unknown observation: " + line);
            }
            observations.push_back(o);
            uint64_t first = o->ticks.Next();
            if (first < durationNs)
            {
                Simulator::Schedule(NanoSeconds(first), &Sample, o);
            }
        }
        else
        {
            Fail("unknown line: " + line);
        }
    } while (std::getline(in, line));

    Simulator::Stop(NanoSeconds(durationNs));
    Simulator::Run();
    out << "events " << Simulator::GetEventCount() << '\n';
    Simulator::Destroy();
    for (Observation* o : observations)
    {
        delete o;
    }
    return 0;
}

} // namespace smol

int
main(int argc, char* argv[])
{
    if (argc != 3)
    {
        std::cerr << "usage: smol-ns3 <scenario> <samples>" << std::endl;
        return 2;
    }
    std::ifstream in(argv[1]);
    if (!in)
    {
        smol::Fail(std::string("cannot read ") + argv[1]);
    }
    std::ofstream out(argv[2]);
    if (!out)
    {
        smol::Fail(std::string("cannot write ") + argv[2]);
    }
    int status = smol::Run(in, out);
    out.flush();
    return out ? status : 2;
}
