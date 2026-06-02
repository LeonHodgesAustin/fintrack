import plaid
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration


_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def create_client(client_id: str, secret: str, env: str) -> plaid_api.PlaidApi:
    if env not in _ENV_MAP:
        raise ValueError(f"Unknown PLAID_ENV '{env}'. Must be one of: {list(_ENV_MAP)}")

    configuration = Configuration(
        host=_ENV_MAP[env],
        api_key={
            "clientId": client_id,
            "secret": secret,
        },
    )
    return plaid_api.PlaidApi(ApiClient(configuration))


def create_link_token(
    client: plaid_api.PlaidApi,
    client_user_id: str,
    client_name: str = "FinTrack",
    link_customization_name: str = "default",
    products: list[str] | None = None,
) -> str:
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    product_list = [Products(p) for p in (products or ["transactions"])]

    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
        client_name=client_name,
        products=product_list,
        country_codes=[CountryCode("US")],
        language="en",
        link_customization_name=link_customization_name,
    )
    response = client.link_token_create(request)
    return response.link_token


def create_update_link_token(
    client: plaid_api.PlaidApi,
    access_token: str,
    client_user_id: str,
    client_name: str = "FinTrack",
    link_customization_name: str = "default",
) -> str:
    """Create a link token for the update/reauth flow."""
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser

    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
        client_name=client_name,
        country_codes=[CountryCode("US")],
        language="en",
        access_token=access_token,
        link_customization_name=link_customization_name,
    )
    response = client.link_token_create(request)
    return response.link_token


def exchange_public_token(
    client: plaid_api.PlaidApi, public_token: str
) -> tuple[str, str]:
    """Returns (access_token, item_id)."""
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )

    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response.access_token, response.item_id


def get_institution_name(client: plaid_api.PlaidApi, access_token: str) -> str:
    """Resolve the human-readable institution name for an item."""
    from plaid.model.country_code import CountryCode
    from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
    from plaid.model.item_get_request import ItemGetRequest

    item_response = client.item_get(ItemGetRequest(access_token=access_token))
    institution_id = item_response.item.institution_id

    inst_response = client.institutions_get_by_id(
        InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode("US")],
        )
    )
    return inst_response.institution.name
