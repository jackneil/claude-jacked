## Rust-Specific

### Tooling
- Linter: cargo clippy (run with --all-targets --all-features).
- Formatter: cargo fmt (rustfmt). No arguments needed.
- Check: cargo check before full builds for faster feedback.

### Style
- Use Result<T, E> for recoverable errors, panic! only for unrecoverable.
- Prefer &str over String in function parameters.
- Use derive macros (Debug, Clone, PartialEq) liberally.
- Document public items with /// doc comments.

### Patterns
- Iterators over manual loops when possible.
- ? operator for error propagation. Avoid unwrap() in library code.
- Builder pattern for complex struct construction.
- Enum + match for state machines. Compiler enforces exhaustiveness.

### Avoid
- unwrap() and expect() in library code. Reserve for tests and examples.
- Clone as a first resort. Understand ownership first.
- Unsafe blocks without a safety comment explaining the invariant.
