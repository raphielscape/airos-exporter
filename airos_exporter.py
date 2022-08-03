#!/usr/bin/env python3
import json
import os
import re
from itertools import takewhile
from sys import stderr
from time import sleep
from typing import Any, Callable, Dict, Iterable, Iterator, List, Union
from urllib.parse import parse_qs

import paramiko
from cached_property import cached_property_with_ttl
from prometheus_client import (CollectorRegistry, Counter, Gauge,
                               generate_latest)
from waitress import serve

# mime="application/openmetrics-text"
mime = "text/plain"


class DictX(Dict):
    "dict with DictX({}) as default value"

    def __missing__(self, key: Any):
        return DictX({})

    def __str__(self) -> str:
        return '' if self == {} else super(DictX, self).__str__()


class Config(DictX):
    def __missing__(self, key: Any) -> Dict[str, str]:
        key = str(key)
        return Config({
            k[len(key)+1:]: v
            for (k, v) in self.items()
            if k.startswith(key + '.')
        })

    def __iter__(self) -> Iterator[Union[str, Dict[str, str]]]:
        if any(k.startswith('0.') for k in self.keys()):
            return takewhile(lambda x: x != {}, (self[i] for i in range(2**32)))
        elif any(k.startswith('1.') for k in self.keys()):
            return takewhile(lambda x: x != {}, (self[i] for i in range(1, 2**32)))
        else:
            return super(Config, self).__iter__()

    # Val_ = Union[str, int, bool, Dict[str, 'Val_']]

    def change(self, key: str, val: Union[str, int, Dict[str, Any]]):
        for k in list(filter(lambda x: x.startswith(key), self.keys())):
            del self[k]
        if isinstance(val, str):
            self[key] = val
        elif isinstance(val, bool):
            self[key] = 'enabled' if val else 'disabled'
        elif isinstance(val, int):
            self[key] = str(val)
        elif isinstance(val, Dict):
            for subkey in val.keys():
                self.change(key + '.' + subkey, val[subkey])
        else:
            raise TypeError

    def __str__(self):
        return "\n".join("{}={}".format(key, self[key]) for key in sorted(self.keys()))


class AirOS(paramiko.SSHClient):

    def __init__(self, hostname: str, password: str, user: str = 'ubnt'):
        super(AirOS, self).__init__()
        # self.load_system_host_keys()
        self.set_missing_host_key_policy(paramiko.AutoAddPolicy)
        self.connect(hostname=hostname, username=user, password=password,
                     timeout=30, banner_timeout=30, auth_timeout=30)

    def json_output(self, command: str) -> Union[Dict, List]:
        _stdin, stdout, _stderr = self.exec_command(command, timeout=20)
        return json.load(stdout, object_hook=lambda dct: DictX(dct))

    @cached_property_with_ttl(ttl=5)
    def status(self) -> Union[Dict, List]:
        return self.json_output('ubntbox status')

    def read_status(self) -> Union[Dict, List]:
        del self.__dict__['status']
        return self.status  # type: ignore

    @cached_property_with_ttl(ttl=5)
    def status_iter(self) -> Iterable[Dict]:
        return self.json_output('ubntbox status')

    def read_status_iter(self) -> Iterable[Dict]:
        del self.__dict__['status']
        return self.status  # type: ignore

    @cached_property_with_ttl(ttl=5)
    def wstalist(self) -> Iterable[Dict]:
        return self.json_output('wstalist')

    def read_wstalist(self) -> Iterable[Dict]:
        del self.__dict__['wstalist']
        return self.wstalist  # type: ignore

    @cached_property_with_ttl(ttl=5)
    def mcastatus(self) -> Dict[str, str]:
        _stdin, stdout, _stderr = self.exec_command(
            'ubntbox mca-status', timeout=20)
        return {
            k: v
            for [k, v] in
            [
                s.split('=', 1)
                for s in re.split('[\r\n,]+', str(stdout.read().decode('UTF-8').strip()))
            ]
        }

    def read_mcastatus(self) -> Dict[str, str]:
        del self.__dict__['mcastatus']
        return self.mcastatus  # type: ignore


def airos_connect(hostname: str, password: str) -> AirOS:
    for _ in range(9):
        try:
            airos = AirOS(hostname=hostname, password=password)
        except paramiko.ssh_exception.AuthenticationException as e:
            raise e
        except paramiko.ssh_exception.SSHException as e:
            print(type(e).__name__)
            sleep(2)
        else:
            return airos
    return AirOS(hostname=hostname, password=password)


def application(environ: Dict, start_response: Callable):
    path = environ['PATH_INFO']
    q = parse_qs(environ.get('QUERY_STRING'))
    target = q.get('target', [None])[0]
    if path != '/metrics' and path != '/metrics/':
        status = "404 Not Found"
        body = b''
        size = 0
    elif not target:
        status = "500 Internal Server Error"
        body = b'No target parameter'
        size = len(body)
    else:
        r = CollectorRegistry()
        rr: List[CollectorRegistry] = [r]
        try:
            with airos_connect(hostname=target, password=UBNT_PASSWORD) as airos:

                labels = {
                    "ap_mac": airos.mcastatus.get('apMac'),
                    "device_id": airos.mcastatus.get('deviceId'),
                    "device_name": airos.mcastatus.get('deviceName'),
                    "wireless_mode": airos.mcastatus.get('wlanOpmode', '')
                }

                # Device Health section
                Gauge("airos_device_load_avg", 'Device Load Average', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("loadavg"))

                memFree = float(airos.mcastatus.get("memFree"))
                memTotal = float(airos.mcastatus.get("memTotal"))
                memUsed = memTotal - memFree
                Gauge("airos_device_ram_usage_percent", 'Device RAM Usage', labels.keys(), registry=r).labels(**labels).set(
                    float(memUsed / memTotal * 100))

                # Wireless section
                Gauge("airos_airmax_quality_percents", 'The airMax Quality (AMQ) is based on the number of retries and '
                      'the quality of the physical link', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("wlanPollingQuality"))

                Gauge("airos_airmax_capacity_percents", 'The airMax Capacity (AMC) is based on the ratio of current rate and maximum '
                      'rate', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("wlanPollingCapacity"))

                Gauge("airos_wlan_tx_rate_mbps", 'Radio TX rate', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("wlanTxRate"))

                Gauge("airos_wlan_rx_rate_mbps", 'Radio RX rate', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("wlanRxRate"))

                Gauge("airos_signal_dbm", 'Signal', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("signal"))

                Gauge("airos_chanbw_mhz", 'Channel Width', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("chanbw"))

                Gauge("airos_center_freq_mhz", 'Frequency', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("centerFreq"))

                Gauge("airos_tx_power_dbm", 'TX Power', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("txPower"))

                Gauge("airos_chain_0_signal_dbm", 'Chan 0 Signal', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("chain0Signal"))

                Gauge("airos_chain_1_signal_dbm", 'Chan 1 Signal', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("chain1Signal"))

                Gauge("airos_noise_dbm", 'Noise Floor', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("noise"))

                Gauge("airos_distance_meter", 'Distance', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("distance"))

                Gauge("airos_lan_plugged", 'LAN plugged', labels.keys(), registry=r).labels(**labels).set(
                    airos.mcastatus.get("lanPlugged"))

                Gauge("airos_ccq_percent", 'CCQ', labels.keys(), registry=r).labels(**labels).set(
                    float(airos.mcastatus.get("ccq")) / 10)

                Gauge("airos_remote_devices", 'Remote Devices', labels.keys(), registry=r).labels(**labels).set(
                    len(airos.wstalist))

                Gauge("airos_remote_devices_extra_reporting", 'Remote Devices with Extra Reporting', labels.keys(), registry=r).labels(**labels).set(
                    len([1 for s in airos.wstalist if "remote" in s]))

                Counter("airos_lan_rx_packets_total", "LAN RX packets", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("lanRxPackets")))

                Counter("airos_lan_tx_packets_total", "LAN TX packets", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("lanTxPackets")))

                Counter("airos_wlan_rx_packets_total", "WLAN RX packets", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("wlanRxPackets")))

                Counter("airos_wlan_tx_packets_total", "WLAN TX packets", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("wlanTxPackets")))

                Counter("airos_lan_rx_bytes_total", "LAN RX bytes", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("lanRxBytes")))

                Counter("airos_lan_tx_bytes_total", "LAN TX bytes", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("lanTxBytes")))

                Counter("airos_wlan_rx_bytes_total", "WLAN RX bytes", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("wlanRxBytes")))

                Counter("airos_wlan_tx_bytes_total", "WLAN TX bytes", labels.keys(), registry=r).labels(**labels).inc(
                    int(airos.mcastatus.get("wlanTxBytes")))

                # AirOS Service Uptime Percentage
                wlanUptime = int(airos.mcastatus.get("wlanUptime"))
                deviceUptime = int(airos.mcastatus.get("uptime"))
                uptimeperc = wlanUptime / deviceUptime * 100
                Gauge("airos_wireless_service_uptime_perc", "Wireless Service Uptime (Percentage)", labels.keys(), registry=r).labels(**labels).set(
                    int(uptimeperc))

                Gauge("airos_antenna_gain_dbm", 'Antenna Gain, dBi', labels.keys(), registry=r).labels(**labels).set(
                    airos.status.get("board", {}).get("radio", [{}])[0].get("antenna", [{}])[0].get("gain", 0))

                status_iter = airos.status.get("interfaces", [{}])[2].get(
                    "wireless", {}).get("utilization", {})

                wlanBusy = status_iter.get("busy")
                rxBusy = status_iter.get("rx_busy")
                txBusy = status_iter.get("tx_busy")
                rxbusyperc = rxBusy / wlanBusy * 100
                txbusyperc = txBusy / wlanBusy * 100
                Gauge("airos_wlan_rx_busy_percentage", 'RX Busy (Percentage)', labels.keys(), registry=r).labels(**labels).set(
                    int(rxbusyperc))

                Gauge("airos_wlan_tx_busy_percentage", 'TX Busy (Percentage)', labels.keys(), registry=r).labels(**labels).set(
                    int(txbusyperc))

                for remote in airos.wstalist:
                    r2 = CollectorRegistry()

                    remote_labels: Dict[str, str] = {}
                    remote_labels['remote_mac'] = remote['mac']
                    remote_labels['remote_lastip'] = remote['lastip']
                    remote_labels['remote_hostname'] = str(remote.get(
                        'remote', {}).get('hostname', remote.get('name', '')))
                    if remote.get('remote', {}).get('platform'):
                        remote_labels['remote_platform'] = remote.get(
                            'remote', {}).get('platform')
                    if remote.get('remote', {}).get('version'):
                        remote_labels['remote_version'] = remote.get(
                            'remote', {}).get('version')

                    labels2 = labels.copy()
                    labels2.update(remote_labels)
                    Gauge("airos_remote_ccq_percent", 'Remote CCQ', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get("ccq"))

                    Gauge("airos_remote_tx_rate_mbps", 'Remote TX Rate', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('tx'))

                    Gauge("airos_remote_rx_rate_mbps", 'Remote TX Rate', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('rx'))

                    Gauge("airos_remote_tx_latency_seconds", 'Remote TX Latency', labels2.keys(), registry=r2).labels(**labels2).set(
                        float(remote.get("tx_latency", 0)) / 1000)

                    Gauge("airos_remote_rssi_dbm", 'Remote RSSI', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get("rssi"))

                    Gauge("airos_remote_tx_power_dbm", 'Remote TX Power', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('txpower'))

                    Gauge("airos_remote_tx_signal_dbm", 'Signal received by remote device', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('signal'))

                    Gauge("airos_remote_noise_floor_dbm", 'Remote Noise Floor', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('noisefloor'))

                    Gauge("airos_remote_distance_meters", 'Remote Distance', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('distance'))

                    Counter("airos_remote_tx_bytes_total", 'Bytes sent by remote device', labels2.keys(), registry=r2).labels(**labels2).inc(
                        remote.get('stats', {}).get('tx_bytes'))

                    Counter("airos_remote_rx_bytes_total", 'Bytes received by remote device', labels2.keys(), registry=r2).labels(**labels2).inc(
                        remote.get('stats', {}).get('rx_bytes'))

                    Gauge("airos_remote_amq", 'Remote airMax Quality', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('airmax', {}).get('quality'))

                    Gauge("airos_remote_amc", 'Remote airMax Capacity', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('airmax', {}).get('capacity'))

                    Gauge("airos_remote_airmax_priority", 'Remote airMax Priority', labels2.keys(), registry=r2).labels(**labels2).set(
                        remote.get('airmax', {}).get('priority'))

                    wlanUptime = int(remote.get('uptime'))
                    deviceUptime = int(airos.mcastatus.get("uptime"))
                    percentage = wlanUptime / deviceUptime * 100
                    Gauge("airos_remote_uptime_perc", "Wireless Service Uptime (Percentage)", labels2.keys(), registry=r2).labels(**labels2).set(
                        int(percentage))

                    rr.append(r2)

                Gauge('airos_error', '', labels.keys(),
                      registry=r).labels(**labels).set(0)

        except Exception as e:
            Gauge('airos_error', '', ['error'],
                  registry=r).labels(error=str(e)).set(1)

        status = "200 OK"
        body = b''.join(generate_latest(registry=reg) for reg in rr)
        size = len(body)

    headers = [
        ('Content-Type', mime),
        ('Content-Length', str(size))
    ]

    start_response(status, headers)
    return [body]


if __name__ == "__main__":
    WORKERS = int(os.environ.get('WORKERS', '8'))
    PORT = int(os.environ.get('PORT', '8890'))
    UBNT_PASSWORD = os.environ.get('UBNT_PASSWORD', 'ubnt')

    print(
        f'Starting at http://0.0.0.0:{PORT}/metrics', file=stderr)
    serve(application, host='0.0.0.0', port=PORT)

    print('Exiting.', file=stderr)
