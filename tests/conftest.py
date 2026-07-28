"""Global test config: forbid any real model request so tests never hit a live LLM.
Overrides with TestModel/FunctionModel bypass this guard."""

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
