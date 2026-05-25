# Agent instructions

-   Always run `pre-commit` before committing and pushing changes
-   To the best of your ability, ensure tests are passing
-   Follow assertion style (actual on left, expected on right)
-   Always mark AI-generated tests with `ai_generated` Pytest marker
-   Attempt to utilize `pytest.mark.parametrize` wherever appropriate to reduce duplication in test cases
-   For API signatures, require keyword arguments for multi-input functions using `(*, ...)`. For any function with exactly one caller-supplied parameter (excluding `self` and `cls`), require positional-only usage with the `/` designator
-   PR titles should be human-readable and in the past tense; they should NOT use conventional commit style
-   Always add new imports to the top of the file rather than locally scoped inside a function; the only exception is if it is needed to avoid a circular dependency
-   For external dependencies, always avoid specific import style (e.g., using `import abc from xyz` keyword) in favor of the generic full import (e.g., `import xyz; xyz.abc`)
-   Every commit you author MUST include a `Co-Authored-By` trailer identifying both your tool name + version and your underlying model + version. Format (replace all `<…>` placeholders with actual values): `Co-Authored-By: <Tool> <tool-version> / <Model> <model-version> <noreply@<vendor-domain>>
-   Avoid using excessive em-dashes, colons, and semi-colons in written text such as documentation. Prefer breaking into separate, shorter sentences instead.
