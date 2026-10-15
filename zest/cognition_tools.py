"""MCP tools and lexical RAG over an immutable, approved in-memory source package."""

from collections import Counter
import math
from pathlib import PurePosixPath

from .cognition_contracts import canonical, text
from .files import FileReadError
from .index import terms, vector
from .structure import _declarations


class PackageTools:
    def limit(self, name, legacy):
        # Old immutable packages retain their historical tool windows.
        return self.envelope.get("policy", {}).get(name, legacy)

    def __init__(self, envelope):
        self.envelope = envelope
        self.files = {source["id"]: source for source in envelope["files"]}
        self.chunks = []
        for source in self.files.values():
            lines = self.lines(source)
            for start in range(0, len(lines), 30):
                end = min(start + 40, len(lines))
                self.chunks.append({"source_id": source["id"], "start_line": start + 1, "end_line": end,
                                    "terms": terms("\n".join(lines[start:end]) + " " + source["evidence"]["path"])})
                if len(self.chunks) >= self.limit("search_chunks", 2000):
                    break
            if len(self.chunks) >= self.limit("search_chunks", 2000):
                break
        frequency = Counter(term for chunk in self.chunks for term in chunk["terms"])
        self.idf = {term: math.log((len(self.chunks) + 1) / (count + 1)) + 1 for term, count in frequency.items()}
        for chunk in self.chunks:
            chunk["vector"] = vector(chunk["terms"], self.idf)

    @staticmethod
    def lines(source):
        lines = source["body"].split("\n")
        if lines[-1] == "":
            lines.pop()
        return lines

    def source(self, source_id):
        if not isinstance(source_id, str) or source_id not in self.files:
            raise FileReadError("cognition_scope_expansion_required", "来源不在批准包内，需要新资料授权。")
        return self.files[source_id]

    def read(self, source_id, start_line=1, end_line=None):
        source = self.source(source_id)
        if type(start_line) is not int or start_line < 1 or (end_line is not None and (type(end_line) is not int or end_line < start_line)):
            raise FileReadError("invalid_line_range", "原文行段无效。")
        if end_line is not None and end_line - start_line >= self.limit("read_max_lines", 200):
            raise FileReadError("line_limit", f"单次原文最多 {self.limit('read_max_lines', 200)} 行。")
        lines = self.lines(source)
        if start_line > max(1, len(lines)):
            raise FileReadError("invalid_line_range", "开始行超过文件范围。")
        end = min(end_line or start_line + self.limit("read_default_lines", 80) - 1, len(lines))
        selected, size = [], 0
        for number in range(start_line, end + 1):
            value = lines[number - 1].removesuffix("\r")
            if size + len(value) + 1 > self.limit("read_chars", 1200):
                if not selected:
                    raise FileReadError("cognition_line_budget", "单行超过工具原文预算，保留缺口。")
                break
            selected.append({"number": number, "text": value})
            size += len(value) + 1
        actual_end = selected[-1]["number"] if selected else None
        result = {"source_id": source_id, "evidence": {**source["evidence"], "start_line": start_line if selected else None,
                                                     "end_line": actual_end}, "lines": selected,
                "text": "\n".join(line["text"] for line in selected),
                "more_after": bool(lines) and (actual_end or 0) < len(lines)}
        if not result["text"]:
            result["observation"] = {"kind": "empty_file" if not source["body"] else "empty_text_window",
                                     "file_size_bytes": len(source["body"].encode()), "can_cite_as_text": False}
        return result

    def list_files(self, query="", offset=0):
        if not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0:
            raise FileReadError("invalid_cognition_tool", "文件查询或偏移无效。")
        found = [source for source in self.files.values() if query.casefold() in source["evidence"]["path"].casefold()]
        matches = []
        for source in found[offset:offset + self.limit("list_page_size", 15)]:
            item = {"id": source["id"], "path": source["evidence"]["path"], "roles": source["roles"]}
            if len(canonical([*matches, item])) > self.limit("catalog_chars", 5000):
                break
            matches.append(item)
        return {"matches": matches, "next_offset": offset + len(matches) if offset + len(matches) < len(found) else None,
                "total_in_scope": len(found)}

    def search(self, query):
        text(query, 200)
        query_vector = vector(terms(query), self.idf)
        ranked = []
        for chunk in self.chunks:
            score = sum(query_vector.get(term, 0) * value for term, value in chunk["vector"].items())
            if score:
                ranked.append((score, chunk))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["source_id"], pair[1]["start_line"]))
        return {"backend": "lexical_tfidf", "matches": [{"source_id": chunk["source_id"],
                 "path": self.files[chunk["source_id"]]["evidence"]["path"], "start_line": chunk["start_line"],
                 "end_line": chunk["end_line"], "score": round(score, 6)} for score, chunk in ranked[:self.limit("search_results", 6)]],
                "chunk_count": len(self.chunks), "chunk_budget": self.limit("search_chunks", 2000),
                "limitation": "search_locations_require_read_for_citations"}

    def symbols(self, query):
        text(query, 200)
        found, skipped = [], 0
        sources = list(self.files.values())
        for source in sources if "symbol_results" in self.envelope.get("policy", {}) else sources[:100]:
            if PurePosixPath(source["evidence"]["path"]).suffix != ".py":
                continue
            try:
                symbols, _ = _declarations(source["body"].encode(), source["evidence"])
                for symbol in symbols:
                    if query.casefold() in symbol["lexical_name"].casefold():
                        found.append({"source_id": source["id"], "name": symbol["lexical_name"],
                                      "start_line": symbol["evidence"]["start_line"], "end_line": symbol["evidence"]["end_line"]})
            except FileReadError:
                skipped += 1
        maximum = self.limit("symbol_results", 10)
        return {"matches": found[:maximum], "truncated": len(found) > maximum, "skipped": skipped,
                "limitation": "python_declarations_not_runtime_behavior"}


def create_package_server(tools):
    from fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    server = FastMCP("Zest approved project materials", mask_error_details=True, strict_input_validation=True)
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    def result(call):
        try:
            return {"ok": True, "data": call()}
        except FileReadError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    @server.tool(annotations=annotations)
    def list_files(query: str = "", offset: int = 0) -> dict:
        return result(lambda: tools.list_files(query, offset))
    @server.tool(annotations=annotations)
    def search(query: str) -> dict:
        return result(lambda: tools.search(query))
    @server.tool(annotations=annotations)
    def read(source_id: str, start_line: int = 1, end_line: int | None = None) -> dict:
        return result(lambda: tools.read(source_id, start_line, end_line))
    @server.tool(annotations=annotations)
    def symbols(query: str) -> dict:
        return result(lambda: tools.symbols(query))
    return server


async def execute_mcp_actions(tools, actions):
    from fastmcp import Client
    output = []
    async with Client(create_package_server(tools)) as client:
        for action in actions:
            if action["tool"] not in ("list_files", "search", "read", "symbols") or not isinstance(action["args"], dict):
                raise FileReadError("invalid_cognition_tool", "调查请求了不允许的工具。")
            try:
                result = await client.call_tool(action["tool"], action["args"])
                value = result.data
                if not isinstance(value, dict) or "ok" not in value:
                    raise FileReadError("invalid_cognition_tool", "工具未返回预期结构。")
            except FileReadError:
                raise
            except Exception:
                value = {"ok": False, "error": {"code": "invalid_cognition_tool", "message": "工具参数未通过检查。"}}
            output.append({"tool": action["tool"], "result": value})
    return output
