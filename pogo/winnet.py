"""Windows networking for all-apps mode: routes, network adapters and DNS servers, read through
the IP Helper API. No command output is parsed, so this works on every Windows language."""
from __future__ import annotations

import ctypes
import functools
import ipaddress
import socket
import struct
from ctypes import wintypes
from dataclasses import dataclass, field

NO_ERROR = 0
ERROR_BUFFER_OVERFLOW = 111
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_DATA = 232
ROUTE_TYPE_INDIRECT = 4   # MIB_IPROUTE_TYPE_INDIRECT: through a gateway
AF_INET = 2
GAA_FLAGS = 0x2 | 0x4     # skip anycast and multicast addresses
IF_OPER_STATUS_UP = 1


class MIB_IPFORWARDROW(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "dwForwardDest", "dwForwardMask", "dwForwardPolicy", "dwForwardNextHop", "dwForwardIfIndex",
        "dwForwardType", "dwForwardProto", "dwForwardAge", "dwForwardNextHopAS", "dwForwardMetric1",
        "dwForwardMetric2", "dwForwardMetric3", "dwForwardMetric4", "dwForwardMetric5")]


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_DNS_SERVER_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_DNS_SERVER_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG), ("Reserved", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_DNS_SERVER_ADDRESS)), ("Address", SOCKET_ADDRESS)]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


# Only the leading fields pogo needs; the list is walked through Next, so the rest can be omitted.
IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)), ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.c_void_p), ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.POINTER(IP_ADAPTER_DNS_SERVER_ADDRESS)),
    ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG), ("Mtu", wintypes.ULONG), ("IfType", wintypes.ULONG), ("OperStatus", ctypes.c_int)]


@functools.lru_cache(maxsize=1)
def _iphlpapi():
    dll = ctypes.WinDLL("iphlpapi", use_last_error=True)
    dll.GetIpForwardTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL]
    dll.GetIpForwardTable.restype = wintypes.DWORD
    dll.GetBestRoute.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(MIB_IPFORWARDROW)]
    dll.GetBestRoute.restype = wintypes.DWORD
    dll.GetAdaptersAddresses.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.ULONG)]
    dll.GetAdaptersAddresses.restype = wintypes.ULONG
    return dll


def _ntoa(value: int) -> str:
    return socket.inet_ntoa(struct.pack("<I", value))   # stored in network byte order


def _aton(ip: str) -> int:
    return struct.unpack("<I", socket.inet_aton(ip))[0]


@dataclass
class Route:
    dest: str
    mask: str
    nexthop: str
    ifindex: int
    type: int
    metric: int


@dataclass
class Adapter:
    index: int
    name: str
    description: str
    up: bool
    dns: list = field(default_factory=list)


def _route(row: MIB_IPFORWARDROW) -> Route:
    return Route(_ntoa(row.dwForwardDest), _ntoa(row.dwForwardMask), _ntoa(row.dwForwardNextHop),
                 int(row.dwForwardIfIndex), int(row.dwForwardType), int(row.dwForwardMetric1))


def routes() -> list[Route]:
    dll = _iphlpapi()
    size = wintypes.ULONG(0)
    rc = dll.GetIpForwardTable(None, ctypes.byref(size), False)
    buffer = None
    for _ in range(5):
        if rc == ERROR_NO_DATA:
            return []
        buffer = ctypes.create_string_buffer(max(size.value, 4096))
        rc = dll.GetIpForwardTable(buffer, ctypes.byref(size), False)
        if rc == NO_ERROR:
            break
        if rc not in (ERROR_INSUFFICIENT_BUFFER, ERROR_NO_DATA):
            raise OSError(rc, "GetIpForwardTable failed")
    else:
        raise OSError(rc, "GetIpForwardTable failed")
    raw = buffer.raw
    count = struct.unpack_from("<I", raw, 0)[0]
    width = ctypes.sizeof(MIB_IPFORWARDROW)
    return [_route(MIB_IPFORWARDROW.from_buffer_copy(raw, 4 + i * width)) for i in range(count)]


def best_route(ip: str) -> Route | None:
    row = MIB_IPFORWARDROW()
    if _iphlpapi().GetBestRoute(_aton(ip), 0, ctypes.byref(row)) != NO_ERROR:
        return None
    return _route(row)


def primary_route() -> tuple[int | None, str | None]:
    """(interface index, gateway) Windows uses for the internet right now."""
    route = best_route("1.1.1.1")
    if route is None:
        return None, None
    indirect = route.type == ROUTE_TYPE_INDIRECT and route.nexthop != "0.0.0.0"
    return route.ifindex, route.nexthop if indirect else None


def has_default_route(ifindex: int, gateway: str | None) -> bool:
    """Is the default route pogo started on still there? (False after switching networks.)"""
    for r in routes():
        if r.dest == "0.0.0.0" and r.mask == "0.0.0.0" and r.ifindex == ifindex:
            if gateway is None or r.nexthop == gateway:
                return True
    return False


def via_gateway(ip: str) -> bool:
    route = best_route(ip)
    return route is not None and route.type == ROUTE_TYPE_INDIRECT


def route_exists(prefix: str, ifindex: int | None = None) -> bool:
    net = ipaddress.ip_network(prefix, strict=False)
    dest, mask = str(net.network_address), str(net.netmask)
    return any(r.dest == dest and r.mask == mask and (ifindex is None or r.ifindex == ifindex) for r in routes())


def adapters() -> list[Adapter]:
    dll = _iphlpapi()
    size = wintypes.ULONG(16384)
    buffer = None
    for _ in range(5):
        buffer = ctypes.create_string_buffer(size.value)
        rc = dll.GetAdaptersAddresses(0, GAA_FLAGS, None, buffer, ctypes.byref(size))
        if rc == NO_ERROR:
            break
        if rc == ERROR_NO_DATA:
            return []
        if rc != ERROR_BUFFER_OVERFLOW:
            raise OSError(rc, "GetAdaptersAddresses failed")
    else:
        raise OSError(ERROR_BUFFER_OVERFLOW, "GetAdaptersAddresses failed")
    found = []
    node = ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while node:
        adapter = node.contents
        dns = []
        server = adapter.FirstDnsServerAddress
        while server:
            address = server.contents.Address
            if address.lpSockaddr and address.iSockaddrLength >= 8:
                raw = ctypes.string_at(address.lpSockaddr, 8)
                if struct.unpack_from("<H", raw)[0] == AF_INET:
                    dns.append(socket.inet_ntoa(raw[4:8]))
            server = server.contents.Next
        found.append(Adapter(int(adapter.IfIndex), adapter.FriendlyName or "", adapter.Description or "",
                             adapter.OperStatus == IF_OPER_STATUS_UP, dns))
        node = adapter.Next
    return found


def dns_servers(skip_index: int | None = None) -> list[str]:
    found: list[str] = []
    for adapter in adapters():
        if not adapter.up or adapter.index == skip_index:
            continue
        for ip in adapter.dns:
            if not ipaddress.ip_address(ip).is_loopback and ip not in found:
                found.append(ip)
    return found


def interface_name(ifindex: int) -> str:
    try:
        for adapter in adapters():
            if adapter.index == ifindex:
                return adapter.name
    except OSError:
        pass
    return f"interface {ifindex}"


def find_adapter(name: str) -> Adapter | None:
    """The live adapter called name (Windows may call a re-created one "name 2")."""
    candidates = [a for a in adapters() if a.up and (a.name == name or a.name.startswith(name + " "))]
    return max(candidates, key=lambda a: a.index) if candidates else None
