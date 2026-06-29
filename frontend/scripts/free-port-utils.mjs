import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export function parseListeningPids(output, port) {
  const targetSuffix = `:${port}`;
  const pids = new Set();

  for (const line of output.split(/\r?\n/)) {
    const parts = line.trim().split(/\s+/);
    if (parts.length < 5 || parts[0].toUpperCase() !== "TCP") {
      continue;
    }

    const [protocol, localAddress, , state, pidText] = parts;
    if (
      protocol.toUpperCase() === "TCP" &&
      state.toUpperCase() === "LISTENING" &&
      localAddress.endsWith(targetSuffix)
    ) {
      const pid = Number(pidText);
      if (Number.isInteger(pid) && pid > 0) {
        pids.add(pid);
      }
    }
  }

  return Array.from(pids);
}

export async function findListeningPids(port, run = execFileAsync) {
  const { stdout } = await run("netstat", ["-ano"], { windowsHide: true });
  return parseListeningPids(stdout, port);
}

export async function stopProcesses(pids, run = execFileAsync, platform = process.platform) {
  for (const pid of pids) {
    if (platform === "win32") {
      await run("taskkill", ["/PID", String(pid), "/F", "/T"], { windowsHide: true });
    } else {
      await run("kill", ["-TERM", String(pid)]);
    }
  }
}

export async function freePort(port, options = {}) {
  const run = options.run ?? execFileAsync;
  const platform = options.platform ?? process.platform;
  const pids = (await findListeningPids(port, run)).filter((pid) => pid !== process.pid);

  await stopProcesses(pids, run, platform);
  return pids;
}
