/** Small TypeScript fixture for indexer smoke tests. */
import { createLogger } from "./log";

const logger = createLogger("sample");

export class Greeter {
    /** Greet someone by name. */
    greet(name: string): string {
        return formatMessage(name);
    }
}

export function formatMessage(name: string): string {
    try {
        return `Hello, ${name}!`;
    } catch (e) {
        logger.error("format failed", e);
        throw e;
    }
}

export function silentDivide(a: number, b: number): number | null {
    try {
        return a / b;
    } catch (e) {
        return null;
    }
}
