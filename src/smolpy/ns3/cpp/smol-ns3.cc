// smol-ns3: the program that runs SMOLPy scenarios on ns-3 (ADR-0004).
//
// It is built once per ns-3 installation by smolpy.ns3, and each run reads a
// scenario that smolpy.ns3 has lowered to plain lines of integers, with every
// time already in integer nanoseconds (Model Semantics §1):
//
//     smol-ns3 1
//     duration <ns>
//     adapter <mac> <ipv4>                   nodes, numbered from 0 in order
//     switch
//     link <a> <b> <speed num> <speed den> <propagation ns> <gap ns>
//     static <switch> <mac> <port>          a switch's MAC table at the start
//     flow <src> <dst ipv4> <udp payload> <delay ns> <period num> <period den>
//     sink <node>                           a node that receives flows
//     observe <kind> <target> <period num> <period den> <device>...
//
// Link i has the devices 2i (on a) and 2i + 1 (on b); a node's ports are
// numbered in the order of its links. Speeds are exact rationals in Mb/s and
// periods in ns.
//
// The output has one line per sample, "<observation> <t ns> <a> <b>", with
// raw integer counters; smolpy.ns3 computes each metric from them with the
// reference engine's formulas, so that every value is rounded once, as there.
//
// What ns-3 contributes is its IPv4/UDP stack; the link and the switch are
// SMOL's own, to give the same Ethernet as the other engines:
// - SmolDevice: a full-duplex Ethernet port (ns-3's CSMA is half duplex).
//   A frame is its IP packet plus header and FCS (18 B), at least 64 B; it
//   occupies the wire for preamble, SFD and frame, and the next frame waits
//   for the 12 B gap (ADR-0010). The egress queue is unbounded and FIFO.
// - SmolSwitch: store-and-forward, learning, flooding unknown destinations to
//   every port but the one a frame came in on, dropping hairpins.
// - Neighbour caches start full: SMOL models no ARP.

#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"

#include <algorithm>
#include <cstdint>
#include <deque>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace smol
{

using namespace ns3;
using u128 = unsigned __int128;

constexpr uint64_t ETHERNET_BYTES = 14 + 4; // header and FCS
constexpr uint64_t MIN_FRAME_BYTES = 64;
constexpr uint64_t PREAMBLE_SFD_BYTES = 8;
constexpr uint64_t WIRE_OVERHEAD_BITS = (8 + 12) * 8; // preamble, SFD and gap

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

// The instants start + round(k * num / den) for k = 1, 2, ... (timebase.instants)
class Instants
{
  public:
    Instants(uint64_t start = 0, uint64_t num = 1, uint64_t den = 1)
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

struct Frame
{
    Ptr<Packet> packet;
    Mac48Address src;
    Mac48Address dst;
    uint16_t protocol = 0;
    uint64_t bytes = 0;
};

struct Window
{
    uint64_t sum = 0;
    uint64_t count = 0;
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

    void Configure(uint64_t speedNum, uint64_t speedDen, uint64_t gapNs, Ptr<SmolChannel> channel)
    {
        m_speedNum = speedNum;
        m_speedDen = speedDen;
        m_gapNs = gapNs;
        m_channel = channel;
    }

    void SetSwitch(SmolSwitch* sw, uint32_t port)
    {
        m_switch = sw;
        m_port = port;
    }

    // A frame at the end of its propagation (SmolChannel).
    void Receive(Frame frame);

    // A frame for the egress queue: sent now if the port is idle.
    void Enqueue(Frame frame)
    {
        m_queue.push_back(std::move(frame));
        if (!m_busy)
        {
            StartNext();
        }
    }

    uint64_t QueueLength() const
    {
        return m_queue.size(); // the frame on the wire is not queued
    }

    uint64_t bitsSent = 0;      // frame bits whose transmission has completed
    uint64_t framesSent = 0;    // those frames
    uint64_t bytesReceived = 0; // frames addressed to this port
    std::vector<Window*> latencyWindows;

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
        frame.bytes = std::max(MIN_FRAME_BYTES, packet->GetSize() + ETHERNET_BYTES);
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
        m_queue.clear();
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

    void StartNext()
    {
        m_busy = true;
        m_sending = std::move(m_queue.front());
        m_queue.pop_front();
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
    uint64_t m_speedNum = 1;
    uint64_t m_speedDen = 1;
    uint64_t m_gapNs = 0;
    std::deque<Frame> m_queue;
    Frame m_sending;
    bool m_busy = false;
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
    if (!m_queue.empty())
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
    OriginTag tag;
    if (!latencyWindows.empty() && frame.packet->PeekPacketTag(tag))
    {
        uint64_t latency = Simulator::Now().GetNanoSeconds() - tag.Created();
        for (Window* w : latencyWindows)
        {
            w->sum += latency;
            ++w->count;
        }
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

    void Configure(Ipv4Address dst, uint32_t payload, Instants instants, uint64_t stopNs)
    {
        m_dst = dst;
        m_payload = payload;
        m_instants = instants;
        m_stopNs = stopNs;
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
        uint64_t t = m_instants.Next();
        if (t < m_stopNs)
        {
            Simulator::Schedule(NanoSeconds(t - Simulator::Now().GetNanoSeconds()),
                                &SmolFlow::Send,
                                this);
        }
    }

    void Send()
    {
        Ptr<Packet> packet = Create<Packet>(m_payload);
        packet->AddPacketTag(OriginTag(Simulator::Now().GetNanoSeconds()));
        m_socket->Send(packet);
        ScheduleNext();
    }

    Ptr<Socket> m_socket;
    Ipv4Address m_dst;
    uint32_t m_payload = 0;
    Instants m_instants;
    uint64_t m_stopNs = 0;
};

NS_OBJECT_ENSURE_REGISTERED(SmolFlow);

enum class Kind
{
    Throughput,
    Latency,
    BytesSent,
    BytesReceived,
    QueueDepth,
    Utilization,
};

struct Observation
{
    uint32_t index;
    Kind kind;
    Ptr<SmolDevice> device;          // an adapter's port
    SmolSwitch* sw = nullptr;        // queue_depth
    std::vector<Ptr<SmolDevice>> in; // utilization: the ports that send to the target
    Window window;                   // latency
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
    }
    *o->out << o->index << ' ' << now << ' ' << a << ' ' << b << '\n';
    uint64_t next = o->ticks.Next();
    if (next < o->stopNs)
    {
        Simulator::Schedule(NanoSeconds(next - now), &Sample, o);
    }
}

[[noreturn]] void
Fail(const std::string& what)
{
    std::cerr << "smol-ns3: " << what << std::endl;
    std::exit(2);
}

int
Run(std::istream& in, std::ostream& out)
{
    Time::SetResolution(Time::NS);

    std::string word;
    uint32_t version = 0;
    if (!(in >> word >> version) || word != "smol-ns3" || version != 1)
    {
        Fail("not a smol-ns3 version 1 scenario");
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
            uint64_t speedNum;
            uint64_t speedDen;
            uint64_t propagationNs;
            uint64_t gapNs;
            s >> a >> b >> speedNum >> speedDen >> propagationNs >> gapNs;
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
                d->Configure(speedNum, speedDen, gapNs, channel);
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
        else if (word == "flow" || word == "sink" || word == "observe")
        {
            break; // the network is complete: IPv4 and neighbour caches come first
        }
        else
        {
            Fail("unknown line: " + line);
        }
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

    // traffic and observations: the line that ended the network is first
    do
    {
        std::istringstream s(line);
        if (!(s >> word))
        {
            continue;
        }
        if (word == "flow")
        {
            uint32_t src;
            std::string dst;
            uint32_t payload;
            uint64_t delayNs;
            uint64_t num;
            uint64_t den;
            s >> src >> dst >> payload >> delayNs >> num >> den;
            if (!s || src >= nodes.GetN() || !portOf[src])
            {
                Fail("bad flow: " + line);
            }
            Ptr<SmolFlow> flow = CreateObject<SmolFlow>();
            flow->Configure(Ipv4Address(dst.c_str()), payload, Instants(delayNs, num, den), durationNs);
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
            uint64_t num;
            uint64_t den;
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
