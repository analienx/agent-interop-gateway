import { spawn } from "node:child_process";
import process from "node:process";

const DEFAULT_MCP_STDIO =
  "C:\\ProgramData\\Analienx\\mcp-gateway\\bin\\mcp-stdio.cmd";

export const READ_TOOLS = {
  foundry_status: {
    description: "Gateway and downstream runner capability health. No secrets.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  list_projects: {
    description: "Discover readable project IDs, planes and capabilities.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  project_context: {
    description: "One-call bootstrap: pointers, bounded files, Git identity.",
    inputSchema: {
      type: "object",
      properties: { project_id: { type: "string" } },
      required: ["project_id"],
      additionalProperties: false,
    },
  },
  read_project_files: {
    description: "Multi-file content read within one project.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        paths: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 64 },
        max_bytes: { type: "integer", minimum: 1, maximum: 2097152 },
      },
      required: ["project_id", "paths"],
      additionalProperties: false,
    },
  },
  list_project_tree: {
    description: "Bounded tree inventory with continuation cursors.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        roots: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 8 },
        max_results: { type: "integer", minimum: 1, maximum: 5000 },
        max_depth: { type: "integer", minimum: 0, maximum: 8 },
        cursor: { type: "string" },
      },
      required: ["project_id"],
      additionalProperties: false,
    },
  },
  search_project: {
    description: "Bounded text search with excerpts and cursors.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        query: { type: "string", minLength: 1, maxLength: 512 },
        roots: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 8 },
        max_results: { type: "integer", minimum: 1, maximum: 500 },
        cursor: { type: "string" },
      },
      required: ["project_id", "query"],
      additionalProperties: false,
    },
  },
  project_repo_status: {
    description: "Git identity and working state for project paths.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        paths: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 8 },
      },
      required: ["project_id"],
      additionalProperties: false,
    },
  },
  create_project_snapshot: {
    description: "Metadata checkpoint for cheap follow-up deltas.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        roots: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 8 },
      },
      required: ["project_id"],
      additionalProperties: false,
    },
  },
  read_project_delta: {
    description: "Changes after a checkpoint; expired snapshots must restart.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        snapshot_id: { type: "string" },
      },
      required: ["project_id", "snapshot_id"],
      additionalProperties: false,
    },
  },
};

function commandPath() {
  return process.env.AIGW_FOUNDRY_MCP_STDIO || DEFAULT_MCP_STDIO;
}

export async function callFoundryTool(name, args = {}, timeoutMs = 20_000) {
  if (!Object.prototype.hasOwnProperty.call(READ_TOOLS, name)) {
    throw new Error(`Foundry tool is not read-allowlisted: ${name}`);
  }

  const command = commandPath();
  const child = spawn(command, [], {
    shell: process.platform === "win32",
    windowsHide: true,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", chunk => { stdout += chunk; });
  child.stderr.on("data", chunk => { stderr += chunk; });

  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }) + "\n");
  child.stdin.write(JSON.stringify({
    jsonrpc: "2.0", id: 2, method: "tools/call",
    params: { name, arguments: args },
  }) + "\n");
  child.stdin.end();

  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`Foundry MCP tool timed out after ${timeoutMs} ms`));
    }, timeoutMs);

    child.once("error", error => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", code => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(`Foundry MCP exited ${code}: ${stderr.trim()}`));
        return;
      }
      try {
        const responses = stdout.split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
        const response = responses.find(item => item.id === 2);
        if (!response) throw new Error("Foundry MCP returned no tools/call response");
        if (response.error) throw new Error(response.error.message || "Foundry MCP call failed");
        resolve(response.result);
      } catch (error) {
        reject(error);
      }
    });
  });

  return result;
}
