"""Canonical primary domain, with a legacy Scene frontmatter fallback."""


def canonical_domain(metadata):
    domain = metadata.get('canonical_domain')
    if not isinstance(domain, str) or not domain.strip():
        domain = metadata.get('domain') or 'general'
        if isinstance(domain, list):
            domain = next((item for item in domain if isinstance(item, str) and item.strip()), 'general')
    return str(domain).strip() or 'general'


def normalize_domain(metadata):
    return {**metadata, 'canonical_domain': canonical_domain(metadata)}
