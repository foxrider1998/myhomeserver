import sys
sys.path.append("/home/ares/Documents/myserver")
import teams_service as ts
import json

target = ts._get_active_page_target()
print("Active target:", target)

# Evaluate document info and inputs
js = """
(() => {
    const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
        id: i.id,
        name: i.name,
        type: i.type,
        placeholder: i.placeholder,
        value: i.value,
        visible: i.offsetParent !== null,
        rect: i.getBoundingClientRect()
    }));
    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a')).map(b => ({
        text: (b.innerText || b.value || '').trim(),
        id: b.id,
        visible: b.offsetParent !== null,
        rect: b.getBoundingClientRect()
    })).filter(b => b.visible && b.text.length > 0 && b.text.length < 30);
    
    return {
        title: document.title,
        url: window.location.href,
        activeElement: document.activeElement ? document.activeElement.tagName + '#' + document.activeElement.id : null,
        inputs: inputs,
        buttons: buttons.slice(0, 10),
        viewport: { width: window.innerWidth, height: window.innerHeight }
    };
})()
"""

res = ts._send_cdp_command("Runtime.evaluate", {"expression": js, "returnByValue": True})
if res and "result" in res and "result" in res["result"]:
    print("Page state:")
    print(json.dumps(res["result"]["result"]["value"], indent=2))
else:
    print("No result from CDP:", res)
