from chat.services.tools import (
    make_list_document_process_tool,
    make_search_documents_tool,
    make_summarize_session_tool,
)


class ToolRegistry:
    """The fixed set of tools a workflow's STEP node can be assigned to, in the visual
    editor's Tools dropdown.

    Every entry maps a stable string identifier (what actually gets stored in
    Agent.workflow_json, e.g. `{"tools": ["search_documents"]}`) to:
      - `label`: human-readable name shown in the editor's dropdown.
      - `build`: a factory taking (company_id, conversation) -> a LangChain tool
        instance. Both existing tool factories only need one of those two arguments
        (search_documents only needs company_id, summarize_session only needs
        conversation) -- build() here normalizes them to the same two-argument shape so
        the graph builder (agents/graph/builder.py) can construct any registered tool
        the same way, regardless of what it actually needs internally.

    Adding a new tool always requires a code change here (per the user's explicit
    choice) -- the editor's dropdown only ever offers tools already registered below,
    it cannot invent arbitrary new ones from the GUI.
    """

    _REGISTRY = {
        'search_documents': {
            'label': 'Search company documents',
            'build': lambda company_id, conversation: make_search_documents_tool(company_id),
        },
        'summarize_session': {
            'label': 'Summarize this conversation',
            'build': lambda company_id, conversation: make_summarize_session_tool(conversation),
        },
        'list_document_process': {
            'label': 'List document processing status',
            'build': lambda company_id, conversation: make_list_document_process_tool(company_id),
        },
    }

    @classmethod
    def choices(cls):
        """[(identifier, label), ...] for populating the editor's Tools dropdown."""
        return [(identifier, entry['label']) for identifier, entry in cls._REGISTRY.items()]

    @classmethod
    def is_valid(cls, identifier):
        return identifier in cls._REGISTRY

    @classmethod
    def build_tool(cls, identifier, company_id, conversation):
        if not cls.is_valid(identifier):
            raise ValueError(f'Unknown tool identifier: {identifier!r}')
        return cls._REGISTRY[identifier]['build'](company_id, conversation)

    @classmethod
    def build_tools(cls, identifiers, company_id, conversation):
        return [cls.build_tool(identifier, company_id, conversation) for identifier in identifiers]
