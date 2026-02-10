## Node/JavaScript/TypeScript-Specific

### Tooling
- TypeScript over plain JavaScript for any non-trivial project.
- Linter: eslint with typescript-eslint plugin.
- Formatter: prettier (let it handle all formatting, don't fight it).
- Package manager: use whatever lockfile exists (package-lock.json/yarn.lock/pnpm-lock.yaml).

### Style
- ESM (import/export) over CommonJS (require/module.exports).
- Async/await over raw Promises over callbacks. Never mix.
- Strict TypeScript: enable strict mode in tsconfig.json.
- Use const by default. let only when mutation needed. Never var.

### Patterns
- Zod for runtime validation of external data (API responses, form input).
- Functional patterns for data transformation (map, filter, reduce).
- Error boundaries in React. try/catch in async functions.
- Environment variables via process.env with validation at startup.

### Avoid
- any type. Use unknown + type narrowing instead.
- Nested ternaries. Use if/else or early returns.
- Default exports (use named exports for better refactoring).
- console.log in production code. Use a structured logger.
