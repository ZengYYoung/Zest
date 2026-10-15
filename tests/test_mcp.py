import unittest
import sys

from tests import test_projects as fixtures
from zest.git import inspect_repository
from zest.mcp_server import create_server
from zest.storage import ProjectRegistry

try:
    from fastmcp import Client
    HAS_MCP = True
except ImportError:
    HAS_MCP = False


@unittest.skipUnless(HAS_MCP, "MCP extra is not installed")
class MCPTest(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def server(self):
        (self.repo / "README.md").write_text("# Demo\nPython CLI\n")
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.project_id = project["id"]
        return create_server(project["id"], self.state_dir)

    async def test_only_read_tools_are_discoverable(self):
        async with Client(self.server()) as client:
            tools = await client.list_tools()
        self.assertEqual({t.name for t in tools}, {"project_overview", "project_management_context", "read_file", "keyword_search", "indexed_search", "confirmed_knowledge", "project_structure", "symbol_lookup"})
        self.assertTrue(all(t.annotations.read_only_hint for t in tools))

    async def test_management_inventory_is_read_only_and_does_not_create_stores(self):
        server = self.server()
        before = {p.name: p.read_bytes() for p in self.state_dir.iterdir() if p.is_file()}
        async with Client(server) as client:
            response = await client.call_tool("project_management_context", {})
        self.assertTrue(response.data["ok"])
        self.assertEqual(response.data["data"]["project_id"], self.project_id)
        self.assertEqual(response.data["data"]["model_calls"], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_dir.iterdir() if p.is_file()})

    async def test_structure_lookup_and_read_preserve_same_source_without_execution(self):
        (self.repo / "app.py").write_text("raise RuntimeError('never execute')\nclass Service:\n    def method(self):\n        return 1\n")
        server = self.server()
        async with Client(server) as client:
            structure = await client.call_tool("project_structure", {})
            found = await client.call_tool("symbol_lookup", {"query": "Service.method"})
            evidence = found.data["data"]["matches"][0]["evidence"]
            read = await client.call_tool("read_file", {key: evidence[key] for key in ("path", "commit", "start_line", "end_line")})
            invalid = await client.call_tool("symbol_lookup", {"query": ""})
        self.assertTrue(structure.data["ok"])
        self.assertEqual(evidence["commit"], structure.data["data"]["commit"])
        self.assertEqual(evidence["content_sha256"], read.data["data"]["content_sha256"])
        self.assertIn("return 1", read.data["data"]["content"])
        self.assertEqual(invalid.data["error"]["code"], "invalid_symbol_query")

    async def test_read_and_search_return_source(self):
        async with Client(self.server()) as client:
            read = await client.call_tool("read_file", {"path": "README.md", "end_line": 2})
            search = await client.call_tool("keyword_search", {"query": "Python"})
        self.assertTrue(read.data["ok"])
        self.assertEqual(read.data["data"]["lines"][1]["text"], "Python CLI")
        self.assertEqual(len(search.data["data"]["matches"]), 1)

    async def test_unsafe_path_and_revision_are_denied(self):
        async with Client(self.server()) as client:
            traversal = await client.call_tool("read_file", {"path": "../secret.py"})
            secret = await client.call_tool("read_file", {"path": ".env"})
            revision = await client.call_tool("read_file", {"path": "README.md", "commit": "HEAD"})
        self.assertFalse(traversal.data["ok"])
        self.assertFalse(secret.data["ok"])
        self.assertEqual(revision.data["error"]["code"], "invalid_commit")

    async def test_missing_index_is_a_structured_result(self):
        async with Client(self.server()) as client:
            result = await client.call_tool("indexed_search", {"query": "Python"})
        self.assertEqual(result.data["error"]["code"], "index_missing")

    async def test_langchain_adapter_discovers_and_invokes_real_tool(self):
        from langchain.mcp import MCPAdapter
        async with MCPAdapter(self.server()) as adapter:
            tools = await adapter.list_tools()
            tool = next(t for t in tools if t.name == "read_file")
            result = await tool.ainvoke({"path": "README.md", "end_line": 2})
        self.assertIn("Python CLI", str(result))

    async def test_real_stdio_subprocess_transport(self):
        self.server()
        config = {"mcpServers": {"zest": {"command": sys.executable,
                  "args": ["-m", "zest", "--state-dir", str(self.state_dir), "mcp", "serve", self.project_id]}}}
        async with Client(config) as client:
            result = await client.call_tool("read_file", {"path": "README.md", "end_line": 2})
        self.assertTrue(result.data["ok"])
        self.assertEqual(result.data["data"]["lines"][1]["text"], "Python CLI")


if __name__ == "__main__":
    unittest.main()
