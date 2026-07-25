---
trigger: always_on
---

---

## trigger: always_on

# REUSE & ENGINEERING RULES

# REUSE PRINCIPLES

- Always search for existing functionality before creating new logic
- Reuse shared services whenever possible
- Reuse existing APIs and database structures
- Follow DRY principle
- Prefer extending existing flows over introducing parallel systems

# THINKING PROTOCOL

- Always analyze before coding
- Do not jump directly to implementation
- Break problems into logical steps
- Validate assumptions before modifying architecture

# CODE DISCIPLINE

- Prefer simplest working solution
- Avoid overengineering
- Keep changes minimal and localized
- Modify existing code instead of rewriting unnecessarily

# VALIDATION RULES

Ensure:

- consistency with architecture.md
- consistency with existing patterns
- no duplicate logic
- no unnecessary abstractions
- no unintended side effects

# FLOW-FIRST MODIFICATION PROTOCOL

Before modifying code:

1. Identify root entry point
2. Trace execution flow recursively
3. Follow:
   - child functions
   - service dependencies
   - API interactions
   - backend handlers
   - DB operations
   - state updates
   - side effects

Never make isolated modifications without understanding connected dependencies.

# DEPENDENCY TRAVERSAL LIMITS

- Stop traversal when chain becomes unrelated to requested functionality
- Avoid tracing framework internals unless required
- Prioritize business logic over infrastructure code
- Focus on execution-critical paths only

Goal:
Maintain deep understanding without exploding context usage.

# SCRIPT-FIRST STRATEGY (SMART MODE)

Use script-based execution ONLY when it significantly reduces repetitive manual edits.

Preferred for:

- bulk cleanup
- large refactors
- multi-file renaming
- shared API/DB updates
- repetitive transformations

Avoid scripts for:

- small UI fixes
- single-file edits
- simple logic changes

# AUTOMATIC EXECUTION STRATEGY

Before making changes:

Step 1:

- Evaluate affected files
- Detect repetitive patterns

Step 2:
Choose method:

IF:

- change affects 3+ files
- OR repetitive transformations exist

THEN:
→ use script-based execution

ELSE:
→ use direct manual modification

# SCRIPT MODE RULES

When script mode is selected:

- Generate precise and minimal scripts
- Clearly describe affected files
- Avoid unrelated modifications
- Execute safely
- Remove temporary scripts after execution (ephemeral mode)

# MANUAL MODE RULES

When manual mode is selected:

- Modify only required code
- Keep edits minimal
- Preserve architecture consistency

# SAFETY RULES

- Never modify unrelated systems
- Never create duplicate APIs
- Never create duplicate DB structures
- Never create duplicate services
- Always reuse existing architecture
- Ask before introducing new system design if uncertainty exists

# FINAL GOAL

Maximize reuse, minimize duplication, reduce token usage, and maintain system-wide consistency.