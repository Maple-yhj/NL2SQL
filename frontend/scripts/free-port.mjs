import { freePort } from "./free-port-utils.mjs";

const port = Number(process.argv[2] ?? 5173);

if (!Number.isInteger(port) || port <= 0) {
  console.error("Usage: node scripts/free-port.mjs <port>");
  process.exit(1);
}

try {
  const stoppedPids = await freePort(port);
  if (stoppedPids.length) {
    console.log(`Stopped process(es) on port ${port}: ${stoppedPids.join(", ")}`);
  } else {
    console.log(`Port ${port} is free.`);
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
