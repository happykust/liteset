import msgspec
from liteset.schemas.base import ApiResponse, ApiListResponse, ErrorResponse, SupersetErrorDetail


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


def test_error_response_sip40():
    detail = SupersetErrorDetail(message="Not found", error_type="NOT_FOUND")
    resp = ErrorResponse(errors=[detail], message="Not found")
    data = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(data, type=ErrorResponse)
    assert decoded.message == "Not found"
    assert len(decoded.errors) == 1
    assert decoded.errors[0].error_type == "NOT_FOUND"


def test_api_response_defaults():
    resp = ApiResponse()
    assert resp.result is None
    assert resp.message is None


def test_api_list_response_defaults():
    resp = ApiListResponse()
    assert resp.result == []
    assert resp.count == 0


def test_error_response_defaults():
    resp = ErrorResponse()
    assert resp.errors == []
    assert resp.message == ""
