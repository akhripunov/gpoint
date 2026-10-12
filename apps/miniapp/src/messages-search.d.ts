import type { MessageThreadDTO } from "../../../packages/contracts/src/index.js";

export function filterMessageThreads(items: MessageThreadDTO[], query: string): MessageThreadDTO[];
