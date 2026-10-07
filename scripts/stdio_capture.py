#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
SECRET_KEY_RE=re.compile(r"(?:password|passwd|pwd|token|secret|authorization|cookie|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?key|refresh[_-]?token)",re.I)
SECRET_NAME=r"(?:password|passwd|pwd|token|secret|api[_-]?key|client[_-]?secret|access[_-]?key)"
QUOTED_INLINE_SECRET_RE=re.compile(rf"(?i)\b({SECRET_NAME})(\s*[:=]\s*)([\"'])(.*?)\3")
INLINE_SECRET_RE=re.compile(rf"(?i)\b({SECRET_NAME})(\s*[:=]\s*)([^\s;\"']+)")
AUTH_HEADER_RE=re.compile(r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)([\"']?)([^\s\"']+)([\"']?)")
OPENAI_KEY_RE=re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
GITHUB_TOKEN_RE=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
AWS_KEY_RE=re.compile(r"\bAKIA[0-9A-Z]{16}\b")
PRIVATE_KEY_RE=re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",re.S)

def redact_text(v):
    v=AUTH_HEADER_RE.sub(lambda m:m.group(1)+m.group(2)+"[REDACTED]"+m.group(4),v)
    v=QUOTED_INLINE_SECRET_RE.sub(lambda m:m.group(1)+m.group(2)+m.group(3)+"[REDACTED]"+m.group(3),v)
    v=INLINE_SECRET_RE.sub(lambda m:m.group(1)+m.group(2)+"[REDACTED]",v)
    v=OPENAI_KEY_RE.sub("[REDACTED_OPENAI_KEY]",v)
    v=GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]",v)
    v=AWS_KEY_RE.sub("[REDACTED_AWS_KEY]",v)
    v=PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]",v)
    return v

def redact(v,key=None):
    if key and SECRET_KEY_RE.search(str(key)):
        return "[REDACTED]"
    if isinstance(v,dict):
        return {k:redact(x,k) for k,x in v.items()}
    if isinstance(v,list):
        return [redact(x) for x in v]
    if isinstance(v,str):
        return redact_text(v)
    return v

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def bounded(obj):
    obj=redact(obj)
    text=json.dumps(obj,ensure_ascii=False,separators=(",",":"))
    raw=text.encode()
    if len(raw)<=MAX_BYTES:
        return text
    return json.dumps({"_capture_truncated":True,"_preview":raw[:MAX_BYTES].decode(errors="replace")},ensure_ascii=False,separators=(",",":"))

def append(path, obj, lock):
    line=json.dumps(obj,ensure_ascii=False,separators=(",",":"))+"\n"
    with lock:
        with open(path,"a",encoding="utf-8") as f:
            f.write(line)
            f.flush()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mcp",required=True)
    ap.add_argument("--capture-file",required=True)
    ap.add_argument("command",nargs=argparse.REMAINDER)
    args=ap.parse_args()
    cmd=args.command
    if cmd and cmd[0]=="--":
        cmd=cmd[1:]
    if not cmd:
        raise SystemExit("target command required")
    Path(args.capture_file).parent.mkdir(parents=True,exist_ok=True)
    lock=threading.Lock()
    pending={}
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,bufsize=0)
    assert proc.stdin and proc.stdout

    def feed_stdin():
        for line in sys.stdin.buffer:
            try:
                obj=json.loads(line.decode("utf-8"))
                if obj.get("method")=="tools/call":
                    params=obj.get("params") or {}
                    rid=str(obj.get("id"))
                    iid=f"{args.mcp}:stdio:{uuid.uuid4()}"
                    pending[rid]=iid
                    safe={"jsonrpc":obj.get("jsonrpc","2.0"),"id":obj.get("id"),"method":"tools/call","params":{"name":params.get("name"),"arguments":params.get("arguments")}}
                    append(args.capture_file,{"kind":"request","interaction_id":iid,"mcp":args.mcp,"rpc_id":obj.get("id"),"rpc_method":"tools/call","tool_name":params.get("name"),"ts":now_iso(),"payload":bounded(safe)},lock)
            except Exception:
                pass
            proc.stdin.write(line); proc.stdin.flush()
        try: proc.stdin.close()
        except Exception: pass

    t=threading.Thread(target=feed_stdin,daemon=True)
    t.start()
    for line in proc.stdout:
        try:
            obj=json.loads(line.decode("utf-8"))
            rid=str(obj.get("id"))
            iid=pending.pop(rid,None)
            if iid and ("result" in obj or "error" in obj):
                safe={"jsonrpc":obj.get("jsonrpc","2.0"),"id":obj.get("id")}
                if "result" in obj: safe["result"]=obj.get("result")
                if "error" in obj: safe["error"]=obj.get("error")
                append(args.capture_file,{"kind":"response","interaction_id":iid,"mcp":args.mcp,"rpc_id":obj.get("id"),"ts":now_iso(),"payload":bounded(safe)},lock)
        except Exception:
            pass
        sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
    return proc.wait()

if __name__=="__main__":
    raise SystemExit(main())
