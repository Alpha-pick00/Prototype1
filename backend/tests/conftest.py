"""PART A - 전체 스위트에 네트워크 차단을 강제한다.

socket.socket 생성 자체를 막으면 asyncio의 이벤트 루프 내부 배관
(Windows ProactorEventLoop의 self-pipe는 루프백 TCP 소켓으로 구현됨)까지
걸려서 무관한 실패가 난다. 그래서 connect/connect_ex만 가로채 루프백
(127.0.0.1/::1) 이외 주소로의 연결 시도를 즉시 예외로 만든다 - 이게
테스트가 실제로 하지 말아야 할 일("바깥으로 나가는 네트워크 호출")이다.

추가(B-3b 배선 중 발견): connect()만 막으면 DNS 조회(getaddrinfo)는 그대로
새나간다 - httpx가 실제 연결을 시도하기 전에 실제 DNS 서버로 진짜 이름
조회를 보내버려서, "네트워크 차단"이 완전하지 않았다(테스트에서 mock이
빠진 채로 search.danawa.com을 실제로 조회하는 걸로 들통났다). getaddrinfo도
같은 기준으로 막는다."""

from __future__ import annotations

import socket

import pytest

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


class NetworkBlockedError(RuntimeError):
    pass


def _host_of(address: object) -> object:
    return address[0] if isinstance(address, tuple) else address


def _guarded_connect(self: socket.socket, address: object) -> object:
    if _host_of(address) not in _ALLOWED_HOSTS:
        raise NetworkBlockedError(
            f"테스트 중 실제 네트워크 연결 시도가 차단됐다: connect({address!r}). "
            "mock이 빠진 코드 경로일 수 있다."
        )
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> object:
    if _host_of(address) not in _ALLOWED_HOSTS:
        raise NetworkBlockedError(
            f"테스트 중 실제 네트워크 연결 시도가 차단됐다: connect_ex({address!r}). "
            "mock이 빠진 코드 경로일 수 있다."
        )
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host not in _ALLOWED_HOSTS:
        raise NetworkBlockedError(
            f"테스트 중 실제 DNS 조회가 차단됐다: getaddrinfo({host!r}, ...). "
            "mock이 빠진 코드 경로일 수 있다."
        )
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


# PART B - settings.rule_based_mode(2026-08-26, 데모 녹화용 .env 스위치)를
# 끈 채로 스위트를 고정한다. app.config.Settings는 다른 설정들과 같은
# 패턴으로 클래스 정의 시점에 os.environ을 한 번만 읽는데, 개발자가 데모
# 녹화 중 backend/.env에 RULE_BASED_MODE=true를 남겨두면 pytest도 그 값을
# 그대로 물려받아 "DeepSeek을 실제로 부르는지" 검증하는 테스트 11개가
# 전부(원인과 무관하게) 깨지는 걸 실측으로 확인했다 - 로컬 .env 상태가
# 테스트 결과를 좌우하면 안 되므로, block_network와 같은 이유로 여기서
# 강제로 고정한다. rule_based_mode 자체를 검증하는 테스트는 개별적으로
# monkeypatch.setattr(settings, "rule_based_mode", True)로 켠다.
@pytest.fixture(autouse=True)
def rule_based_mode_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "rule_based_mode", False)
