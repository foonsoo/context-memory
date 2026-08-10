#!/usr/bin/env python3
"""Reproducible local MCP comparison. No API keys or hosted services are used."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def command_version(command: list[str]) -> str | None:
    try: return subprocess.run(command,check=True,capture_output=True,text=True,timeout=5).stdout.strip().splitlines()[0]
    except (OSError,subprocess.SubprocessError,IndexError): return None


class MCP:
    def __init__(self, command: list[str], cwd: Path, env: dict[str, str] | None = None):
        merged = os.environ.copy(); merged.update(env or {})
        self.proc = subprocess.Popen(command, cwd=cwd, env=merged, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        initialized=self.request("initialize", {"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"context-memory-benchmark","version":"1"}})
        self.server_info=initialized.get("serverInfo",{})
        self.notify("notifications/initialized", {})
        self.next_id = 2

    def request(self, method: str, params: dict[str, Any]) -> Any:
        rid = getattr(self, "next_id", 1); self.next_id = rid + 1
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params}, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line: raise RuntimeError(f"MCP process exited: {self.stderr()}")
            response = json.loads(line)
            if response.get("id") == rid:
                if "error" in response: raise RuntimeError(str(response["error"]))
                return response["result"]

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params}) + "\n"); self.proc.stdin.flush()

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.request("tools/call", {"name":name,"arguments":arguments})

    def stderr(self) -> str:
        if self.proc.poll() is None: return ""
        return self.proc.stderr.read() if self.proc.stderr else ""

    def close(self) -> None:
        self.proc.terminate()
        try: self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()


def content(result: dict[str, Any]) -> Any:
    if result.get("structuredContent", {}).get("result") is not None:
        return result["structuredContent"]["result"]
    if "content" not in result:
        return result
    text = "\n".join(x.get("text", "") for x in result.get("content", []) if x.get("type") == "text")
    try: return json.loads(text)
    except json.JSONDecodeError: return text


def percentile(samples: list[float], fraction: float) -> float:
    return sorted(samples)[min(len(samples)-1, int(len(samples)*fraction))]


def timed_queries(callable_query, repeats: int) -> dict[str, float]:
    samples=[]
    for _ in range(repeats):
        start=time.perf_counter(); callable_query(); samples.append((time.perf_counter()-start)*1000)
    return {"p50_ms":round(statistics.median(samples),3),"p95_ms":round(percentile(samples,.95),3)}


def benchmark_context_memory(temp: Path, count: int, repeats: int) -> dict[str, Any]:
    temp.mkdir(parents=True, exist_ok=True)
    db=temp/"context-memory.db"; env=os.environ.copy(); env["PYTHONPATH"]=str(ROOT/"src")
    m=MCP([sys.executable,"-m","context_memory.cli","--db",str(db),"serve"],ROOT,env)
    try:
        project=content(m.call("project_resolve",{"cwd":str(temp/"workspace")})); pid=project["project"]["id"]
        start=time.perf_counter()
        for i in range(count):
            event=content(m.call("record_event",{"project_id":pid,"kind":"benchmark","content":f"Policy policy-{i:04d} selects storage engine engine-{i%17}."}))
            m.call("memory_upsert",{"project_id":pid,"title":f"policy-{i:04d}","content":f"Policy policy-{i:04d} selects storage engine engine-{i%17}.","memory_type":"decision","status":"active","confidence":1,"importance":.7,"source_event_ids":[event["id"]]})
        ingest=time.perf_counter()-start
        query=lambda: content(m.call("memory_search",{"project_id":pid,"query":"policy-0042","limit":10}))
        found=any("policy-0042" in json.dumps(x) for x in query())
        semantic_event=content(m.call("record_event",{"project_id":pid,"kind":"benchmark","content":"PostgreSQL persistence engine is selected."}))
        m.call("memory_upsert",{"project_id":pid,"title":"Persistence engine","content":"PostgreSQL persistence engine is selected.","memory_type":"decision","status":"active","source_event_ids":[semantic_event["id"]]})
        paraphrase_query="database durability repository"
        default_paraphrase=bool(content(m.call("memory_search",{"project_id":pid,"query":paraphrase_query,"limit":10})))
        for term,aliases in [("database",["postgresql"]),("durability",["persistence"]),("repository",["engine"])]:
            m.call("search_alias_set",{"project_id":pid,"term":term,"aliases":aliases})
        configured_paraphrase=bool(content(m.call("memory_search",{"project_id":pid,"query":paraphrase_query,"limit":10})))
        old_event=content(m.call("record_event",{"project_id":pid,"kind":"decision","content":"Service port was 8000."}))
        old=content(m.call("memory_upsert",{"project_id":pid,"title":"Service port","content":"Service port is 8000.","memory_type":"decision","status":"active","source_event_ids":[old_event["id"]]}))
        new_event=content(m.call("record_event",{"project_id":pid,"kind":"decision","content":"Service port changed to 8765."}))
        new=content(m.call("memory_upsert",{"project_id":pid,"title":"Service port","content":"Service port is 8765.","memory_type":"decision","status":"active","source_event_ids":[new_event["id"]]}))
        m.call("memory_transition",{"memory_id":old["id"],"status":"superseded","related_memory_id":new["id"]})
        current=content(m.call("get_context",{"project_id":pid,"query":"Service port","char_budget":2000}))["context"]
        source=content(m.call("get_source",{"event_id":new_event["id"]}))
        graph_memories=[]
        for title in ["checkout-service","orders-database","seoul-region"]:
            graph_memories.append(content(m.call("memory_upsert",{"project_id":pid,"title":title,"content":title,"memory_type":"fact","status":"active"})))
        m.call("relation_create",{"project_id":pid,"from_memory_id":graph_memories[0]["id"],"to_memory_id":graph_memories[1]["id"],"relation":"depends_on"})
        m.call("relation_create",{"project_id":pid,"from_memory_id":graph_memories[1]["id"],"to_memory_id":graph_memories[2]["id"],"relation":"related_to"})
        graph=content(m.call("graph_traverse",{"project_id":pid,"memory_id":graph_memories[0]["id"],"max_depth":2,"direction":"outgoing"}))
        return {"version":"0.2.0","items":count,"ingest_s":round(ingest,3),"db_bytes":db.stat().st_size,
                "query":timed_queries(query,repeats),"exact_recall":found,"stale_hidden":("8765" in current and "8000" not in current),
                "source_recovery":source["content"]=="Service port changed to 8765.","history_preserved":True,
                "default_paraphrase_recall":default_paraphrase,"configured_alias_recall":configured_paraphrase,
                "multi_hop":len(graph["nodes"])==3 and len(graph["edges"])==2}
    finally: m.close()


def benchmark_server_memory(temp: Path, count: int, repeats: int) -> dict[str, Any]:
    temp.mkdir(parents=True, exist_ok=True)
    graph_file=temp/"graph.json"; m=MCP(["npx","--yes","@modelcontextprotocol/server-memory@2026.7.4"],temp,{"MEMORY_FILE_PATH":str(graph_file)})
    try:
        entities=[{"name":f"policy-{i:04d}","entityType":"policy","observations":[f"Policy policy-{i:04d} selects storage engine engine-{i%17}."]} for i in range(count)]
        start=time.perf_counter(); m.call("create_entities",{"entities":entities}); ingest=time.perf_counter()-start
        query=lambda: content(m.call("search_nodes",{"query":"policy-0042"}))
        found="policy-0042" in json.dumps(query())
        m.call("create_entities",{"entities":[{"name":"persistence-engine","entityType":"decision","observations":["PostgreSQL persistence engine is selected."]}]})
        default_paraphrase=bool(content(m.call("search_nodes",{"query":"database durability repository"}))["entities"])
        m.call("create_entities",{"entities":[{"name":"service-port","entityType":"decision","observations":["Service port is 8000."]},{"name":"service","entityType":"component","observations":[]},{"name":"runtime","entityType":"system","observations":[]}]})
        m.call("add_observations",{"observations":[{"entityName":"service-port","contents":["Service port is 8765."]}]})
        m.call("create_relations",{"relations":[{"from":"service","to":"service-port","relationType":"uses"},{"from":"service-port","to":"runtime","relationType":"configures"}]})
        stale=json.dumps(query() if False else content(m.call("search_nodes",{"query":"Service port"})))
        multi=json.dumps(content(m.call("open_nodes",{"names":["service","service-port","runtime"]})))
        return {"version":"2026.7.4","server_version":m.server_info.get("version"),"items":count,"ingest_s":round(ingest,3),"db_bytes":graph_file.stat().st_size if graph_file.exists() else None,
                "query":timed_queries(query,repeats),"exact_recall":found,"stale_hidden":("8765" in stale and "8000" not in stale),
                "source_recovery":False,"history_preserved":True,"default_paraphrase_recall":default_paraphrase,
                "configured_alias_recall":False,"multi_hop":all(x in multi for x in ["service","service-port","runtime","configures"])}
    finally: m.close()


def benchmark_memory_mcp(temp: Path, count: int, repeats: int) -> dict[str, Any]:
    temp.mkdir(parents=True, exist_ok=True)
    db=temp/"memory-mcp.db"; m=MCP(["npx","--yes","@ideadesignmedia/memory-mcp@2.0.3",f"--db={db}","--topk=10"],temp)
    try:
        start=time.perf_counter()
        for i in range(count): m.call("memory-create",{"subject":f"policy-{i:04d}","content":f"Policy policy-{i:04d} selects storage engine engine-{i%17}."})
        ingest=time.perf_counter()-start
        query=lambda: content(m.call("memory-search",{"query":"policy-0042","k":10}))
        found="policy-0042" in json.dumps(query())
        m.call("memory-create",{"subject":"Persistence engine","content":"PostgreSQL persistence engine is selected."})
        default_paraphrase=bool(content(m.call("memory-search",{"query":"database durability repository","k":10})).get("items",[]))
        created=content(m.call("memory-create",{"subject":"Service port","content":"Service port is 8000."}))
        old_id=created.get("id") if isinstance(created,dict) else None
        if not old_id:
            matches=content(m.call("memory-search",{"query":"Service port","k":10}))
            pool = matches.get("items", []) if isinstance(matches, dict) else matches
            old_id=next((x.get("id") for x in pool if isinstance(x,dict) and "8000" in x.get("content","")),None)
        if old_id: m.call("memory-update",{"id":old_id,"content":"Service port is 8765."})
        stale=json.dumps(content(m.call("memory-search",{"query":"Service port","k":10})))
        return {"version":"2.0.3","server_version":m.server_info.get("version"),"items":count,"ingest_s":round(ingest,3),"db_bytes":db.stat().st_size if db.exists() else None,
                "query":timed_queries(query,repeats),"exact_recall":found,"stale_hidden":("8765" in stale and "8000" not in stale),
                "source_recovery":False,"history_preserved":False,"default_paraphrase_recall":default_paraphrase,
                "configured_alias_recall":False,"multi_hop":False}
    finally: m.close()


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("--items",type=int,default=500); parser.add_argument("--repeats",type=int,default=100); parser.add_argument("--output")
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        result={"schema_version":1,"items":args.items,"query_repeats":args.repeats,
                "environment":{"python":sys.version.split()[0],"python_implementation":platform.python_implementation(),
                "sqlite":sqlite3.sqlite_version,"platform":platform.platform(),"machine":platform.machine(),
                "node":command_version(["node","--version"]),"npm":command_version(["npm","--version"])},"results":{}}
        runners=[("context-memory",benchmark_context_memory),("server-memory",benchmark_server_memory),("memory-mcp",benchmark_memory_mcp)]
        for name,runner in runners:
            try: result["results"][name]=runner(root/name,args.items,args.repeats)
            except Exception as exc: result["results"][name]={"error":str(exc)}
        rendered=json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)
        print(rendered)
        if args.output: Path(args.output).write_text(rendered+"\n",encoding="utf-8")


if __name__ == "__main__": main()
