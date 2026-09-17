import { callFoundryTool } from "./mcp-client.mjs";

try {
  const result = await callFoundryTool("foundry_status", {}, 10_000);
  const content = result?.content ?? [];
  if (!Array.isArray(content) || content.length === 0) {
    throw new Error("Foundry MCP status returned no content");
  }
  process.stdout.write(JSON.stringify({
    ok: true,
    transport: "local_mcp",
    tool: "foundry_status",
  }) + "\n");
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`Foundry MCP probe failed: ${message}\n`);
  process.exitCode = 1;
}
