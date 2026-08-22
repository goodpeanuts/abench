// pi-emsctl-tool.mjs — pi 扩展：把任务环境收敛为唯一工具 emsctl
//
// 由 scripts/run-pi-harness.sh 以 --extension 显式加载，配合
// --tools emsctl --no-extensions 使用：模型没有任何文件读写、shell 或
// 网络工具，唯一能力是调用本工具，而本工具只以固定 argv（不经过 shell）
// 在 main 容器内执行一个 emsctl 子命令。
//
// 环境变量（由 harness 注入）：
//   PI_EMSCTL_BRIDGE_ARGS — 逗号分隔的 compose 参数，如
//     "-f,/abs/docker-compose.yaml,-f,/abs/docker-compose.local.yaml,-p,sffd-pi"
import { Type } from "typebox";

const ALLOWED_SUBCOMMANDS = new Set([
  "observe",
  "dispatch",
  "reassign",
  "reposition",
  "advance",
  "finish",
  "--help",
]);
// 直连模式/全局旗子一律拒绝：sidecar 模式下它们要么非法，要么会把请求
// 引向容器外的状态目录。子命令自身的旗子（--incident/--unit/…）原样放行。
const FORBIDDEN_FLAGS = new Set(["--scenario", "--state-dir", "--json", "--fresh"]);
const EMSCTL_TIMEOUT_MS = 120_000;

export default function emsctlToolExtension(pi) {
  const bridgeEnv = process.env.PI_EMSCTL_BRIDGE_ARGS || "";
  const bridgeArgs = bridgeEnv.split(",").map((part) => part.trim()).filter(Boolean);
  if (bridgeArgs.length === 0) {
    throw new Error("pi-emsctl-tool: 未设置 PI_EMSCTL_BRIDGE_ARGS（应由 run-pi-harness.sh 注入）");
  }

  pi.registerTool({
    name: "emsctl",
    label: "emsctl",
    description:
      "与任务环境交互的唯一接口。每次调用恰好执行一个 emsctl 子命令（在隔离容器内、经固定桥接），" +
      "并把该子命令输出的 JSON 对象原样返回。子命令：observe / dispatch / reassign / " +
      "reposition / advance / finish。",
    promptSnippet: "Run one emsctl subcommand against the SFFD task environment per call",
    promptGuidelines: [
      "Use emsctl as the only way to interact with the task environment; there are no file or shell tools.",
    ],
    parameters: Type.Object({
      args: Type.Array(Type.String(), {
        description:
          'emsctl 参数数组，第一项必须是子命令。例：["observe"]；' +
          '["dispatch","--incident","I002","--unit","AM207","--state-version","2","--action-id","d-101"]；' +
          '["advance","--next-event"]；["finish","--reason","all feasible work complete"]',
      }),
    }),
    async execute(_toolCallId, params, signal) {
      const argv = (params.args || []).map((value) => String(value));
      if (argv.length === 0) {
        throw new Error("args 不能为空：第一项必须是 emsctl 子命令");
      }
      const subcommand = argv[0];
      if (!ALLOWED_SUBCOMMANDS.has(subcommand)) {
        throw new Error(
          `不允许的子命令：${subcommand}（允许：${[...ALLOWED_SUBCOMMANDS].join(" / ")}）`
        );
      }
      for (const arg of argv) {
        if (FORBIDDEN_FLAGS.has(arg)) {
          throw new Error(
            `不允许的全局旗子：${arg}（--scenario/--state-dir/--json/--fresh 已被 harness 禁用）`
          );
        }
      }
      // 固定前缀 + token 化参数，不经 shell，模型无法注入额外命令。
      const full = ["compose", ...bridgeArgs, "exec", "-T", "main", "emsctl", ...argv];
      const result = await pi.exec("podman", full, { signal, timeout: EMSCTL_TIMEOUT_MS });
      const stdout = (result.stdout || "").trim();
      const stderr = (result.stderr || "").trim();
      if (result.code !== 0) {
        throw new Error(
          `emsctl 退出码 ${result.code}\nstdout: ${stdout.slice(0, 2000)}\nstderr: ${stderr.slice(0, 2000)}`
        );
      }
      return {
        content: [{ type: "text", text: stdout || "(no output)" }],
        details: { code: result.code, subcommand },
      };
    },
  });
}
