"""bpkb — Best Practices Knowledge Base (curated external practices -> gap analysis -> decision points D1-D12).

Practice record schema (see schema.py validate_practice):
    id             str   unique, 'bp.<source>.<slug>'
    source         str   framework/doc id (matches a sources.json entry id)
    pillar         str   reliability | security | cost | operational-excellence | sre
    statement      str   the practice, one sentence
    status         str   compliant | gap | na
    evidence       str   what was checked (check id + observed value)
    decision_point str   'D1'..'D12' or 'none'
    links          list  related second-brain nodes: 'finding:..', 'incident:..', 'decision:..'
    sensitivity    str   always 'public' at ingestion (external-derived, sanitized)
"""
