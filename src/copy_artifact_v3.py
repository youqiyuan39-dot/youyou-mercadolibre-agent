"""Reuse validated copy independently of downstream category/attribute failures."""
from .task_evidence_v3 import digest
def signature(draft,model):
    from .ai_listing_pipeline import sanitized_product_context,compact_copy_context
    from .prompt_policy_v3 import PROMPT_VERSION
    context=compact_copy_context(sanitized_product_context(draft))
    facts={k:context.get(k) for k in ('supplier_title','supplier_description','supplier_attributes','selected_variant','packaging','compatibility_evidence')}
    selected=context.get('selected_sku') or {}
    facts['selected_sku']={k:selected.get(k) for k in ('sku_id','variant','package','supplier_facts')}
    return digest({'prompt_version':PROMPT_VERSION,'model':model,'source_facts':facts})

def load(record,draft,model,quality):
    proposal=record.get('proposal')
    if not isinstance(proposal,dict) or record.get('copy_signature')!=signature(draft,model):return None
    if record.get('paid_api_used') or record.get('provider')!='local':return None
    if quality(proposal,draft):return None
    return proposal
