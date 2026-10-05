import type { IncomingMessage, ServerResponse } from "node:http";

export function assertProxyConfig(): void;
export function proxyTarget(url: string, base: string, prefix: string): URL | null;
export function mediaStream(target: URL | null, method: string | undefined): string | null;
export function handleProxy(req: IncomingMessage, res: ServerResponse): boolean;
