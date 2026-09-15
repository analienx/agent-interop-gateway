import xml.etree.ElementTree as ET

from agent_interop_gateway.android_adb import AndroidAdb
from agent_interop_gateway.android_relay import format_result
from agent_interop_gateway.chatgpt_android import looks_like_delegation


def test_ui_node_parsing():
    element = ET.fromstring(
        '<node text="Send" content-desc="Send message" class="android.widget.Button" '
        'resource-id="send" package="com.openai.chatgpt" editable="false" '
        'clickable="true" bounds="[10,20][110,80]" />'
    )
    bridge = AndroidAdb()
    node = bridge._node_from_element(element)
    assert node.center == (60, 50)
    assert node.clickable


def test_delegation_hint_is_natural_language():
    assert looks_like_delegation("Can you run this locally on my computer?")
    assert looks_like_delegation("Delegate this to the local machine")
    assert not looks_like_delegation("Tell me a joke")


def test_relay_result_formatting():
    rendered = format_result({"state": "succeeded", "stdout": "done"})
    assert rendered == "[local delegation result] done"
