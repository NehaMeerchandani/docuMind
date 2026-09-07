from pydantic import BaseModel, Field

from agents.models import Agent
from chat.services.llm_client import get_chat_model
from chat.services.prompt_service import PromptService

ROUTER_PROMPT_SLUG = 'agent-router'

FALLBACK_ROUTER_PROMPT = (
    'You are a routing assistant. Given the user\'s message and a list of available '
    'agents (each with a name and a description of what it handles), pick the single '
    'agent best suited to answer this specific message. If nothing matches well, pick '
    'the agent that seems most general-purpose rather than leaving it blank.'
)


class RouterService:
    """Decides, on every single message (not once per conversation, per the user's
    explicit choice), which Agent should handle it.

    Uses an LLM call with structured output constrained to the actual set of currently
    saved agent names (via a Pydantic Literal-like enum built at call time), so the
    model can only ever pick a real agent -- it can't hallucinate a name that doesn't
    exist. The router's own instructions are themselves a Langfuse-managed prompt
    (slug: agent-router), consistent with the rest of this project's prompt management.
    """

    @classmethod
    async def choose_agent(cls, question):
        agents = [agent async for agent in Agent.objects.all()]

        if not agents:
            return None
        if len(agents) == 1:
            return agents[0]

        agent_names = [agent.name for agent in agents]
        agent_directory = '\n'.join(f'- {agent.name}: {agent.description or "(no description)"}' for agent in agents)

        class RouterDecision(BaseModel):
            agent_name: str = Field(description=f'Must be exactly one of: {", ".join(agent_names)}')

        router_prompt, config = PromptService.get_prompt_and_config(
            ROUTER_PROMPT_SLUG, fallback=FALLBACK_ROUTER_PROMPT,
        )

        llm = get_chat_model(temperature=config.get('temperature', 0.0))
        # method='function_calling' (tool-calling under the hood) rather than the
        # default 'json_schema'/'json_mode' -- DeepSeek's API doesn't support the
        # response_format modes with_structured_output would otherwise default to
        # ("This response_format type is unavailable now"), but it does support tool
        # calling, which is exactly what our own tools already rely on.
        structured_llm = llm.with_structured_output(RouterDecision, method='function_calling')

        decision = await structured_llm.ainvoke([
            {'role': 'system', 'content': f'{router_prompt}\n\nAvailable agents:\n{agent_directory}'},
            {'role': 'user', 'content': question},
        ])

        chosen_name = decision.agent_name
        for agent in agents:
            if agent.name == chosen_name:
                return agent

        # The model returned something outside the allowed set despite the constrained
        # schema (rare, but structured output isn't a hard guarantee with every
        # provider) -- fall back to the first agent rather than failing the message.
        return agents[0]
