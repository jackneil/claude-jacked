## Go-Specific

### Tooling
- Linter: go vet (always) + golangci-lint (recommended).
- Formatter: gofmt (non-negotiable — Go's standard formatter).
- Test: go test ./... with -race flag for race detection.

### Style
- Accept interfaces, return structs.
- Check every error. if err != nil is correctness, not boilerplate.
- Short variable names in small scopes (i, n, err). Descriptive in larger scopes.
- Package names: short, lowercase, no underscores.

### Patterns
- Table-driven tests for comprehensive coverage.
- Context propagation for cancellation and deadlines.
- Functional options pattern for configurable constructors.
- Channels for communication, mutexes for state protection.

### Avoid
- init() functions. Explicit initialization in main or constructors.
- Goroutine leaks. Always ensure goroutines can exit.
- Interface pollution. Define interfaces where they're used, not implemented.
