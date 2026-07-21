import json, urllib.request

HOST = "http://localhost:8000"
raw = open("/opt/data/auto-browser/.env").read()
TOKEN = raw.split("API_BEARER_TOKEN=")[1].split("\n")[0].strip()
SID = "3027a614d36f"

def mcp_call(name, args):
    req = json.dumps({"name": name, "arguments": args}).encode()
    opener = urllib.request.build_opener()
    reqobj = urllib.request.Request(
        f"{HOST}/mcp/tools/call", data=req,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    )
    try:
        resp = opener.open(reqobj, timeout=30).read().decode()
        return json.loads(resp)
    except Exception as e:
        return {"_error": str(e)}

js = (
    "var s=function(n,v){var e=document.querySelector(n);if(!e)return n+':NO';"
    "e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));"
    "e.dispatchEvent(new Event('change',{bubbles:true}));return n+':OK'};"
    "var out=[];"
    "out.push(s(\"input[name=app_title]\",\"CloudStorage\"));"
    "out.push(s(\"input[name=app_shortname]\",\"cloudstore\"));"
    "out.push(s(\"input[name=app_url]\",\"https://example.com\"));"
    "var d=document.querySelector(\"input[name=app_platform][value=desktop]\");"
    "if(d){d.click();out.push('plat:OK')}else{out.push('plat:NO')}"
    "out.push(s(\"textarea[name=app_desc]\",\"Personal cloud storage\"));"
    "out.join('|')"
)
res = mcp_call("browser.eval_js", {"session_id": SID, "expression": js})
print("FILL:", json.dumps(res)[:400])
