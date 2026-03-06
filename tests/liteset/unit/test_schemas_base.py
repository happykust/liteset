import msgspec
from liteset.schemas.base import ApiResponse, ApiListResponse, ErrorResponse


def test_api_response():
    resp = ApiResponse(result={"id": 1, "name": "test"})
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ApiResponse)
    assert decoded.result == {"id": 1, "name": "test"}


def test_api_list_response():
    resp = ApiListResponse(result=[{"id": 1}, {"id": 2}], count=2)
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ApiListResponse)
    assert decoded.count == 2
    assert len(decoded.result) == 2


def test_error_response():
    resp = ErrorResponse(message="Not found", errors={"id": ["Invalid"]})
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ErrorResponse)
    assert decoded.message == "Not found"
    assert "id" in decoded.errors


def test_api_response_defaults():
    resp = ApiResponse()
    assert resp.result is None
    assert resp.message is None


def test_api_list_response_defaults():
    resp = ApiListResponse()
    assert resp.result == []
    assert resp.count == 0
