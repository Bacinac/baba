import http from "node:http";
import { handler } from "./build/handler.js";
import { assertProxyConfig, handleProxy } from "./proxy.mjs";

assertProxyConfig();
const PORT = Number(process.env.PORT || 5173);
const server = http.createServer((req, res) => {
  if (handleProxy(req, res)) return;
  if (req.url === "/favicon.ico") res.setHeader("content-type", "image/x-icon");
  handler(req, res);
});

server.listen(PORT, "0.0.0.0", () => console.log(`baba-web (production) listening on :${PORT}`));
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.once(signal, () => {
    server.close(() => process.exit(0));
    server.closeAllConnections();
  });
}
