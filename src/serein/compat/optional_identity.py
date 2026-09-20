from ..deployment import expanded_identity, DEFAULT_IDENTITY


def generic_identity_names():
    return identity_names({'identity':DEFAULT_IDENTITY})


def identity_names(config):
    names=expanded_identity({**DEFAULT_IDENTITY, **config.get('identity',{})})
    return {**names,'user_aliases_text':names['user_name']}


def render_identity_template(template,names):
    for key,value in names.items():
        if isinstance(value,str):template=template.replace('{'+key+'}',value)
    return template
