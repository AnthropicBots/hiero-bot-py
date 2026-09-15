import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.dependencies import get_current_user, get_current_user_optional
from app.db.database import Base, get_db
from app.db.models import User
from app.main import app


@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_db):
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_current_user_optional] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def authed_client(test_db):
    test_user = User(id=1, github_user_id=1001, github_login="test_user")
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_current_user_optional] = lambda: test_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_response_carries_a_csp_header(client):
    r = await client.get("/healthz")
    assert "content-security-policy" in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_csp_restricts_default_src_to_self(client):
    r = await client.get("/healthz")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp


@pytest.mark.asyncio
async def test_csp_blocks_object_and_framing(client):
    r = await client.get("/healthz")
    csp = r.headers["content-security-policy"]
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.asyncio
async def test_csp_script_src_allows_only_self_nonce_and_jsdelivr(client):
    r = await client.get("/healthz")
    csp = r.headers["content-security-policy"]
    directives = {d.strip().split(" ")[0]: d.strip() for d in csp.split(";") if d.strip()}
    script_src = directives["script-src"]
    assert "'self'" in script_src
    assert "https://cdn.jsdelivr.net" in script_src
    assert "'nonce-" in script_src
    assert "'unsafe-inline'" not in script_src


@pytest.mark.asyncio
async def test_csp_style_src_allows_only_self_nonce_and_google_fonts(client):
    r = await client.get("/healthz")
    csp = r.headers["content-security-policy"]
    directives = {d.strip().split(" ")[0]: d.strip() for d in csp.split(";") if d.strip()}
    style_src = directives["style-src"]
    assert "'self'" in style_src
    assert "https://fonts.googleapis.com" in style_src
    assert "'nonce-" in style_src
    assert "'unsafe-inline'" not in style_src


@pytest.mark.asyncio
async def test_csp_nonce_differs_per_request(client):
    r1 = await client.get("/healthz")
    r2 = await client.get("/healthz")

    def nonce_of(csp: str) -> str:
        for part in csp.split(";"):
            part = part.strip()
            if part.startswith("script-src"):
                for token in part.split(" "):
                    if token.startswith("'nonce-"):
                        return token
        return ""

    n1 = nonce_of(r1.headers["content-security-policy"])
    n2 = nonce_of(r2.headers["content-security-policy"])
    assert n1 and n2
    assert n1 != n2


@pytest.mark.asyncio
async def test_existing_security_headers_are_preserved(client):
    r = await client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"


@pytest.mark.asyncio
async def test_login_page_style_block_uses_the_response_nonce(client):
    r = await client.get("/")
    assert r.status_code == 200

    csp = r.headers["content-security-policy"]
    nonce = None
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("style-src"):
            for token in part.split(" "):
                if token.startswith("'nonce-"):
                    nonce = token[len("'nonce-"):-1]
    assert nonce
    assert f'nonce="{nonce}"' in r.text


@pytest.mark.asyncio
async def test_dashboard_page_script_and_style_blocks_use_the_response_nonce(
    authed_client,
):
    r = await authed_client.get("/")
    assert r.status_code == 200

    csp = r.headers["content-security-policy"]
    nonce = None
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("script-src"):
            for token in part.split(" "):
                if token.startswith("'nonce-"):
                    nonce = token[len("'nonce-"):-1]
    assert nonce
    assert r.text.count(f'nonce="{nonce}"') >= 2


@pytest.mark.asyncio
async def test_dashboard_page_has_no_inline_event_handler_attributes(authed_client):
    r = await authed_client.get("/")
    assert r.status_code == 200

    import re

    assert not re.search(r'\bon(click|change|keyup)="', r.text)


@pytest.mark.asyncio
async def test_chartjs_script_tag_has_integrity_and_is_not_the_dynamic_min_build(
    authed_client,
):
    r = await authed_client.get("/")
    assert r.status_code == 200

    import re

    m = re.search(r'<script src="[^"]*chart\.umd[^"]*"[^>]*>', r.text)
    assert m
    tag = m.group(0)

    assert "chart.umd.js" in tag
    assert "chart.umd.min.js" not in tag
    assert 'integrity="sha384-' in tag
    assert 'crossorigin="anonymous"' in tag