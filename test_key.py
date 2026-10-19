import sys
sys.path.append("/home/ares/Documents/myserver")
import teams_service as ts

js = """
(() => {
    const el = document.querySelector('input[type="email"], input[name="loginfmt"], input[type="text"]');
    if (el) {
        el.focus();
        el.value = '';
        el.dispatchEvent(new Event('input', {bubbles:true}));
        return true;
    }
    return false;
})()
"""
ts._send_cdp_command("Runtime.evaluate", {"expression": js, "returnByValue": True})
ts.teams_dispatch_key(text="arisqa1@westpro.my.id")
open("/tmp/test_after_typing.jpg", "wb").write(ts.teams_get_screenshot())
print("Filled email and saved screenshot!")
