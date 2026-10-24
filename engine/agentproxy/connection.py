import time

from .flowlog import clean
from .recorder import CONNECT_FAILED, TLS_CLIENT_ERROR

DEFAULT_MINUTES = 5
MAX_MINUTES = 24 * 60
RUN_GAP_SECONDS = 120
SCAN_LIMIT = 20000
TOP_HOSTS = 5
SAMPLE_CHARS = 200
TLS_CLIENT_PREFIX = TLS_CLIENT_ERROR.split("{")[0]
TLS_VERSION_MARK = "cannot agree on a TLS version"
SERVER_CERTIFICATE_MARK = "Certificate verify failed"
TLS_DROPPED_MARKS = ("disconnected during the handshake", "Cannot parse ClientHello")
CLOSED_EARLY_MARK = "connection closed early"
CLIENT_GAVE_UP_MARK = ": client disconnected"
CANCELLED_CONNECT_MS = 3000
FAILURE_KINDS = ("tls_untrusted", "tls_dropped", "tls_version", "connect_failed", "server_certificate")
QUIET_DEVICE_PROBLEMS = ("adb not found", "no device")
TLS_KINDS = ("tls_untrusted", "tls_dropped", "tls_version")
PAUSED_FOR_CHARLES = ("AgentProxy is off while Charles runs (tray setting «Выключать прокси, пока запущен Charles»): "
                      "it turns back on by itself when Charles is closed and puts the device proxy back on devices it took "
                      "it off, if they are still connected with no proxy; if AgentProxy is needed now, ask the user to "
                      "close Charles or untick the setting in «Настройки…»: unticking restarts the engine without putting "
                      "the device proxy back, so run device_proxy enable after it")
APP_RESTART = ("restart the app (many HTTP clients read the proxy only at start) and do something in it that sends "
               "requests, then check_connection again; requests of other apps but none of this one: the app ignores "
               "the system proxy or pins certificates (OkHttp CertificatePinner closes the connection without a "
               "record)")


def classify(summary):
    error = summary["error"] or ""
    if error.startswith(TLS_CLIENT_PREFIX):
        if CLOSED_EARLY_MARK in error:
            return "cancelled" if (summary["duration_ms"] or 0) < CANCELLED_CONNECT_MS else "connect_failed"
        if TLS_VERSION_MARK in error:
            return "tls_version"
        return "tls_dropped" if any(mark in error for mark in TLS_DROPPED_MARKS) else "tls_untrusted"
    if summary["method"] == "CONNECT":
        if error.startswith(CONNECT_FAILED) and CLIENT_GAVE_UP_MARK in error and (
                summary["duration_ms"] or 0) < CANCELLED_CONNECT_MS:
            return "cancelled"
        return "connect_failed" if error else "request"
    if SERVER_CERTIFICATE_MARK in error:
        return "server_certificate"
    return "request"


def local_time(timestamp):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def flowing_since(times):
    if not times:
        return None
    ordered = sorted(times, reverse=True)
    since = ordered[0]
    for started in ordered[1:]:
        if since - started > RUN_GAP_SECONDS:
            break
        since = started
    return since


def _top_hosts(counts):
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_HOSTS]
    return [{"host": host, "count": count} for host, count in ranked]


def _failure_hosts(hosts):
    ranked = sorted(hosts.items(), key=lambda item: (-item[1]["count"], item[0]))[:TOP_HOSTS]
    return [{"host": host, "count": entry["count"], "last": local_time(entry["last"])} for host, entry in ranked]


def _host_list(failure):
    return ", ".join(entry["host"] for entry in failure["hosts"])


def summarize(rows, now, minutes):
    requests = {"count": 0, "https": 0, "http": 0, "failed": 0}
    request_hosts = {}
    https_hosts = set()
    decrypted = {}
    failures = {}
    first = last = None
    request_times = []
    record_times = []
    window_start = now - minutes * 60
    for summary in rows:
        started = summary["started"]
        kind = classify(summary)
        if kind == "cancelled":
            continue
        record_times.append(started)
        if kind in ("request", "server_certificate"):
            request_times.append(started)
        if started < window_start and not summary["open"]:
            continue
        if kind in ("request", "server_certificate"):
            first = started if first is None else min(first, started)
            last = started if last is None else max(last, started)
            requests["count"] += 1
            requests["https" if summary["scheme"] == "https" else "http"] += 1
            if summary["scheme"] == "https":
                https_hosts.add(summary["host"])
                decrypted[summary["host"]] = max(decrypted.get(summary["host"], started), started)
            if summary["error"]:
                requests["failed"] += 1
            request_hosts[summary["host"]] = request_hosts.get(summary["host"], 0) + 1
            if kind == "request":
                continue
        entry = failures.setdefault(kind, {}).setdefault(summary["host"], {"count": 0, "last": started, "sample": None})
        entry["count"] += 1
        if started >= entry["last"] or entry["sample"] is None:
            entry["last"] = started
            entry["sample"] = clean(summary["error"], SAMPLE_CHARS)
    traffic = dict(requests, https_hosts=len(https_hosts), top_hosts=_top_hosts(request_hosts), window_minutes=minutes,
                   first=local_time(first) if first is not None else None,
                   last=local_time(last) if last is not None else None)
    newest = max(record_times) if record_times else None
    traffic["last_record"] = local_time(newest) if newest is not None else None
    traffic["last_record_seconds_ago"] = max(int(now - newest), 0) if newest is not None else None
    since = flowing_since(request_times)
    traffic["flowing_since"] = (local_time(since) if since is not None and now - max(request_times) <= RUN_GAP_SECONDS
                                else None)
    ordered = []
    for kind in FAILURE_KINDS:
        hosts = failures.get(kind, {})
        if kind in TLS_KINDS:
            hosts = {host: entry for host, entry in hosts.items() if decrypted.get(host, 0) <= entry["last"]}
        if not hosts:
            continue
        latest = max(hosts.values(), key=lambda entry: entry["last"])
        ordered.append({"kind": kind, "count": sum(entry["count"] for entry in hosts.values()),
                        "hosts": _failure_hosts(hosts), "host_count": len(hosts), "last": local_time(latest["last"]),
                        "sample": latest["sample"]})
    return traffic, ordered


def certificate_refused(traffic, failures):
    untrusted = next((failure for failure in failures if failure["kind"] == "tls_untrusted"), None)
    return untrusted is not None and not traffic["https_hosts"]


def verdict(listening, traffic, failures, device, paused=None):
    kinds = {failure["kind"] for failure in failures}
    if paused:
        return "paused_for_charles"
    if not listening:
        return "proxy_not_listening"
    if certificate_refused(traffic, failures) or (not traffic["https_hosts"] and kinds & {"tls_dropped", "tls_version"}):
        return "tls_refused"
    if traffic["count"]:
        return "traffic_flows"
    if "connect_failed" in kinds:
        return "hosts_unreachable"
    if device.get("checked") and not device.get("ready"):
        return "device_not_routed"
    return "no_traffic"


def failure_hints(failures, traffic):
    hints = []
    refused = certificate_refused(traffic, failures)
    for failure in failures:
        hosts = _host_list(failure)
        kind = failure["kind"]
        if kind == "tls_untrusted" and refused:
            hints.append("{0} HTTPS hosts refuse the AgentProxy certificate ({1}) and none is decrypted. Only system "
                         "services (Google Play and the like, they never accept user certificates) and no host of the "
                         "app under test: the app sends nothing through the proxy yet, restart it first. Hosts of the "
                         "app among them: the certificate is not installed on the device or the client does not trust "
                         "user certificates. "
                         "The user installs it by hand (http://mitm.it in the device browser with the proxy on), the "
                         "app must trust user certificates (debug build, network_security_config); then restart the "
                         "app".format(failure["host_count"], hosts))
        elif kind == "tls_untrusted":
            hints.append("{0} HTTPS hosts refuse the AgentProxy certificate ({1}), {2} decrypted (list_flows shows "
                         "which). System services (Google Play and the like) refuse user certificates always; if hosts "
                         "of the app under test are among them, the certificate is not installed on this device "
                         "(http://mitm.it), the app does not trust user certificates (network_security_config), pins "
                         "the certificate or uses another HTTP client, and their requests stay invisible".format(
                             failure["host_count"], hosts, traffic["https_hosts"]))
        elif kind == "tls_dropped":
            hints.append("the client dropped the TLS handshake or sent no TLS for {0} (mitmproxy text in sample): if "
                         "it repeats for the hosts of the app under test, the certificate is refused: the user "
                         "installs it (http://mitm.it in the device browser with the proxy on), the app must trust "
                         "user certificates (network_security_config)".format(hosts))
        elif kind == "tls_version":
            hints.append("the client and the proxy cannot agree on a TLS version for {0}: an old Android or "
                         "client; check on a newer Android or an emulator".format(hosts))
        elif kind == "connect_failed":
            hints.append("the proxy could not reach {0} (reason in sample): from this PC the host is unreachable "
                         "(VPN, DNS, firewall), not a fault of the app; a mock for such a host needs a rule "
                         "with a host condition (agentproxy skill, rules.md)".format(hosts))
        else:
            hints.append("the server certificate of {0} failed verification (incomplete chain or an internal CA): "
                         "the tray setting «Проверять сертификаты серверов» («Настройки…») turns the check off for all hosts, "
                         "only with the user's consent and back on after the task".format(hosts))
    return hints


def build_hints(result, port):
    traffic = result["traffic"]
    device = result["device"]
    hints = []
    if result["verdict"] == "paused_for_charles":
        hints.append(PAUSED_FOR_CHARLES)
        return hints
    if result["verdict"] == "proxy_not_listening":
        hints.append("the proxy is not listening on port {0}: read_log (source engine) shows PORT BUSY or a crash; "
                     "a program holding the port (often Charles) has to be closed, or the user changes the port in "
                     "Settings".format(port))
        if device.get("device_proxy") == device.get("expected_proxy") and device.get("checked"):
            hints.append("the device points to the stopped proxy and has no internet: device_proxy disable until the "
                         "proxy runs")
        return hints
    reached = bool(traffic["count"] or result["failures"])
    if device.get("problem") and not (reached and device.get("problem_kind") in QUIET_DEVICE_PROBLEMS):
        hints.append("device not checked: " + device["problem"])
    elif device.get("checked") and not device.get("ready") and device.get("hint"):
        hints.append(device["hint"])
    hints.extend(failure_hints(result["failures"], traffic))
    if traffic["count"]:
        hints.append("traffic goes through the proxy{0}: read it with list_flows".format(
            " since " + traffic["flowing_since"] if traffic["flowing_since"] else ""))
        if device.get("ready") and result["verdict"] == "traffic_flows":
            hints.append("if the hosts of the app under test are not in list_flows: " + APP_RESTART)
        return hints
    if traffic["last_record"] is None:
        hints.append("the history is empty: nothing has reached the proxy yet")
    elif not result["failures"]:
        hints.append("nothing recorded in the last {0} min, the last record is from {1}".format(
            traffic["window_minutes"], traffic["last_record"]))
    if device.get("ready") and result["verdict"] == "no_traffic":
        hints.append("the device is routed: " + APP_RESTART)
    elif not device.get("checked") and not reached:
        hints.append("point the client to 127.0.0.1:{0}: an Android device with device_proxy enable, a browser or "
                     "program on this PC in its proxy settings".format(port))
    return hints
