import io
import os
import re
from threading import Thread
from urllib.parse import urlparse

import ncclient.manager
import paramiko
import pytest
from ncclient.transport.session import BASE_NS_1_0, to_ele
from ncclient.transport.ssh import MSG_DELIM
from scapy.layers.snmp import SNMPvarbind

from mb_netmgmt import mb, netconf, ssh, use_scalar_strings, yaml
from mb_netmgmt.__main__ import create_server, get_cli_patterns

port = 8081
prompt = b"prompt#"
mock_response = f"""<rpc-reply xmlns="{BASE_NS_1_0}">
  <blubb/>
</rpc-reply>"""
cli_responses = [
    (b"\rIOS-1>", True),
    (b"\rIOS-1#", True),
    (b"\rIOS-1(config)#", True),
    (b"\rIOS-1(config-if)#", True),
    (b"\rRP/0/8/CPU0:IOSXR-2>", True),
    (b"\rRP/0/8/CPU0:IOSXR-2#", True),
    (b"\rRP/0/8/CPU0:IOSXR-2(config)#", True),
    (b"\rRP/0/8/CPU0:IOSXR-2(config-if)#", True),
    (
        b"\rKUncommitted changes found, commit them before exiting(yes/no/cancel)? [cancel]:",
        True,
    ),
    (b"\rProtocol [ipv4]: ", True),
    (b"\rTarget IP address: ", True),
    (b"\rHost name or IP address (control-c to abort): []?", True),
    (b"\rDestination file name (control-c to abort): [running-config]?", True),
    (b"\rDelete net/node0_8_CPU0/disk0:/c12k-mini.vm-4.3.2[confirm]", True),
    (b"\rDestination filename [/net/node0_8_CPU0/disk0:/c12k-mini.vm-4.3.2]?", True),
    (b"\rReload hardware module ? [no,yes] ", True),
    (b"\rDo you wish to continue?[confirm(y/n)]", True),
    (b"\r --More-- ", True),
    (b"\rSending 5, 100-byte ICMP Echos to 8.8.8.8, timeout is 2 seconds:", False),
]


@pytest.mark.parametrize("protocol", ["http", "snmp", "telnet", "netconf"])
def test_create_imposter(protocol):
    with mb(imposter(protocol, None)):
        pass


def test_ssh():
    with mb(imposter("ssh", [prompt_stub()]), "debug"):
        client = connect_ssh()
        chan = client.invoke_shell()
        out = chan.recv(1024)
        assert out == prompt


def test_ssh_proxy():
    with mb(
        [
            {"protocol": "ssh", "port": 2222, "stubs": [prompt_stub()]},
            {
                "protocol": "ssh",
                "port": port,
                "stubs": [
                    {
                        "responses": [
                            {
                                "proxy": {
                                    "to": f"ssh://{os.environ['NETCONF_USERNAME']}:{os.environ['NETCONF_PASSWORD']}@localhost:2222"
                                }
                            },
                        ]
                    },
                ],
            },
        ]
    ):
        client = connect_ssh()
        chan = client.invoke_shell()
        out = chan.recv(1024)
        assert out == prompt


def prompt_stub():
    return {"responses": [{"is": {"response": prompt.decode()}}]}


def test_create_ssh_server():
    server = create_server(ssh, port, None)
    Thread(target=server.serve_forever).start()
    connect_ssh().close()
    server.shutdown()


def connect_ssh():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy)
    client.connect("localhost", port)
    return client


def test_create_netconf_server():
    port = 8830
    original_open_upstream = netconf.Handler.open_upstream
    original_post_request = netconf.Handler.post_request
    netconf.Handler.open_upstream = lambda handler: None
    netconf.Handler.post_request = mock_post_request
    server = create_server(netconf, port, None)
    Thread(target=server.serve_forever).start()
    with ncclient.manager.connect(
        host="localhost", port=port, password="", hostkey_verify=False
    ) as m:
        assert m.session_id == "1"
        assert "urn:ietf:params:netconf:base:1.0" in m.server_capabilities
        m.get_config("running")
    netconf.stopped = True
    server.shutdown()
    netconf.stopped = False
    netconf.Handler.open_upstream = original_open_upstream
    netconf.Handler.post_request = original_post_request


def mock_post_request(handler, request):
    return {"response": {"rpc-reply": mock_response}}


def test_netconf_upstream():
    from mb_netmgmt.netconf import Handler

    global port
    port = 8831
    Handler.handle = lambda handler: None
    Handler.get_to = lambda handler: urlparse(f"netconf://localhost:{port}")
    handler = Handler(None, None, None)
    handler.save_key({})
    with mb(
        imposter(
            "netconf",
            [
                {
                    "predicates": [{"endsWith": {"rpc": ">running</get-config>"}}],
                    "responses": [
                        {"is": {"rpc-reply": mock_response}},
                    ],
                }
            ],
        ),
        "debug",
    ):
        handler.open_upstream()
        handler.send_upstream(
            {"rpc": "<get-config>running</get-config>"},
            42,
        )
        proxy_response = handler.read_proxy_response()
        assert proxy_response["rpc-reply"] == mock_response


def imposter(protocol, stubs=None):
    return [{"protocol": protocol, "port": port, "stubs": stubs}]


def create_proxy_response(message_id):
    return {
        "response": f'<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" message-id="{message_id}"/>'
        + MSG_DELIM.decode()
    }


def test_remove_message_id():
    result = netconf.remove_message_id(
        to_ele(f'<rpc-reply xmlns="{BASE_NS_1_0}" message-id="42"><blubb/></rpc-reply>')
    )
    assert result == mock_response


def test_netconf_default_response():
    with mb(imposter("netconf")):
        with ncclient.manager.connect(
            host="localhost", port=port, password="", hostkey_verify=False
        ) as m:
            m.get_config("running")


def test_netconf_private_key():
    with mb(
        [
            {"protocol": "netconf", "port": 830, "stubs": []},
            {
                "protocol": "netconf",
                "port": port,
                "stubs": [
                    {
                        "responses": [
                            {
                                "proxy": {
                                    "to": f"netconf://{os.environ['NETCONF_USERNAME']}@localhost",
                                    "key": os.environ["NETCONF_KEY"],
                                }
                            },
                        ]
                    },
                ],
            },
        ]
    ):
        ncclient.manager.connect(
            host="localhost", port=port, password="", hostkey_verify=False
        )


@pytest.mark.parametrize(
    ["base", "result"],
    [
        ({"x": "y\nz"}, "x: |-\n  y\n  z\n"),
        ({"x": "y\r\nz"}, 'x: "y\\r\\nz"\n'),
    ],
)
def test_use_scalar_strings(base, result):
    use_scalar_strings(base)

    s = io.StringIO()
    yaml.dump(base, s)
    s.seek(0)
    assert s.read() == result


@pytest.mark.parametrize("cli_response,result", cli_responses)
def test_cli_patterns(cli_response, result):
    matched = False
    for pattern in get_cli_patterns():
        if re.findall(pattern, cli_response):
            matched = True
            break
    assert matched == result


def test_snmp_no_such_instance():
    pkt = b"0\x10\x06\x0c+\x06\x01\x02\x01/\x01\x01\x01\x01\n\x01\x81\x00"
    result = SNMPvarbind(pkt)
    assert not result.noSuchObject
    assert result.noSuchInstance
    assert not result.endOfMibView
    assert bytes(result) == pkt
