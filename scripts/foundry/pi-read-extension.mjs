import { READ_TOOLS, callFoundryTool } from "./mcp-client.mjs";

export default function foundryReadExtension(pi) {
  for (const [name, definition] of Object.entries(READ_TOOLS)) {
    pi.registerTool({
      name,
      label: `Foundry · ${name}`,
      description: definition.description,
      promptSnippet: definition.description,
      promptGuidelines: [
        "Use Foundry tools for all machine and repository evidence.",
        "Never claim to have changed files or machine state: this tool surface is read-only.",
        "Prefer project_context for the first read of a known project and snapshot/delta for follow-ups.",
      ],
      parameters: definition.inputSchema,
      async execute(_toolCallId, params) {
        const result = await callFoundryTool(name, params);
        return {
          content: result.content ?? [{ type: "text", text: JSON.stringify(result) }],
          details: { transport: "local_mcp", mcpTool: name },
        };
      },
    });
  }
}
