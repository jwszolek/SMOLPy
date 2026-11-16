// Smoke test of the ns-3 base image (ADR-0004, Container images).
//
// Built as a scratch program, as the ns-3 backend's generated scenarios will
// be: two hosts on CSMA links to a learning bridge, the SMOL wire model of a
// link (data rate, propagation delay, a 20 B inter-frame gap), neighbour
// caches filled before the run, and a UDP echo across the bridge. Exits
// non-zero unless every echo comes back.
//
//     cp smoke.cc $NS3_HOME/scratch/ && $NS3_HOME/ns3 run smoke

#include "ns3/applications-module.h"
#include "ns3/bridge-module.h"
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"

#include <iostream>

using namespace ns3;

static uint32_t g_echoes = 0;

static void
OnEcho(Ptr<const Packet>)
{
    ++g_echoes;
}

int
main(int argc, char* argv[])
{
    const uint32_t packets = 10;
    Time::SetResolution(Time::NS); // the SMOL clock: integer nanoseconds

    NodeContainer hosts;
    hosts.Create(2);
    Ptr<Node> sw = CreateObject<Node>();

    // one CSMA channel per link: 100 Mb/s, 10 m at 2e8 m/s
    CsmaHelper csma;
    csma.SetChannelAttribute("DataRate", DataRateValue(DataRate("100Mbps")));
    csma.SetChannelAttribute("Delay", TimeValue(NanoSeconds(50)));

    NetDeviceContainer hostDevices;
    NetDeviceContainer switchPorts;
    for (uint32_t i = 0; i < hosts.GetN(); ++i)
    {
        NetDeviceContainer link = csma.Install(NodeContainer(hosts.Get(i), sw));
        // CSMA attaches with a 12 B gap and no preamble: 20 B at 100 Mb/s
        // stand for both (ADR-0010). The gap is not an attribute, so it is
        // set on each device once it is attached.
        for (uint32_t d = 0; d < link.GetN(); ++d)
        {
            DynamicCast<CsmaNetDevice>(link.Get(d))->SetInterframeGap(NanoSeconds(1600));
        }
        hostDevices.Add(link.Get(0));
        switchPorts.Add(link.Get(1));
    }
    BridgeHelper bridge;
    bridge.Install(sw, switchPorts);

    InternetStackHelper internet;
    internet.Install(hosts);
    Ipv4AddressHelper ipv4;
    ipv4.SetBase("10.0.0.0", "255.255.255.0");
    Ipv4InterfaceContainer addresses = ipv4.Assign(hostDevices);
    // SMOL models no ARP: the neighbour caches start full, or the first
    // frames would wait up to 10 ms for a jittered request (RequestJitter)
    NeighborCacheHelper neighbours;
    neighbours.PopulateNeighborCache();

    UdpEchoServerHelper server(9);
    ApplicationContainer serverApp = server.Install(hosts.Get(1));
    serverApp.Start(MilliSeconds(0));

    UdpEchoClientHelper client(addresses.GetAddress(1), 9);
    client.SetAttribute("MaxPackets", UintegerValue(packets));
    client.SetAttribute("Interval", TimeValue(MilliSeconds(1)));
    client.SetAttribute("PacketSize", UintegerValue(512));
    ApplicationContainer clientApp = client.Install(hosts.Get(0));
    clientApp.Start(MilliSeconds(1));
    Config::ConnectWithoutContext("/NodeList/*/ApplicationList/*/$ns3::UdpEchoClient/Rx",
                                  MakeCallback(&OnEcho));

    Simulator::Stop(MilliSeconds(100));
    Simulator::Run();
    Simulator::Destroy();

    std::cout << "ns-3 smoke test: " << g_echoes << " of " << packets << " echoes" << std::endl;
    return g_echoes == packets ? 0 : 1;
}
