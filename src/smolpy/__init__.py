from importlib.metadata import PackageNotFoundError, version

from smolpy.batch import BatchResult, Run, run_many
from smolpy.core.network import Network
from smolpy.ethernet.switch import Frames
from smolpy.modbus.slave import ModbusSlave
from smolpy.mqtt.broker import MQTTBroker
from smolpy.profinet.device import ProfinetDevice

try:
    __version__ = version("smolpy")  # the installed distribution's, from git at build time
except PackageNotFoundError:  # run from a source tree that is not installed
    __version__ = "0+unknown"

__all__ = [
    "BatchResult",
    "Frames",
    "MQTTBroker",
    "ModbusSlave",
    "Network",
    "ProfinetDevice",
    "Run",
    "run_many",
]
