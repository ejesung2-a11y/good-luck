"""
storage.py - Cloudflare R2(S3 호환) 영구 저장소 + 로컬 폴백
 * 로컬 data/ 폴더를 '작업 사본'으로 쓰고, 변경될 때마다 R2 의 <prefix> 아래로 올립니다.
 * 앱이 재시작돼 data/ 가 비면, 시작할 때 R2 에서 자동 복원합니다.
 * 안전장치: 이 모듈은 <prefix> 밖의 키는 절대 읽기/쓰기/삭제하지 않습니다 (같은 버킷을 다른 프로그램이 써도 안전).
 설정(.streamlit/secrets.toml 또는 Streamlit Cloud > Settings > Secrets):
   [r2]
   account_id = "..."          # 대시보드 주소의 긴 해시
   access_key_id = "..."
   secret_access_key = "..."
   bucket = "ajin-info-files"
   prefix = "quantum-lab/"     # 생략 가능 (기본값)
"""
import os
import streamlit as st

DATA_DIR = "data"
_state = {"restored": False, "error": ""}


def _cfg():
    try:
        c = st.secrets["r2"]
        if all(k in c for k in ("account_id", "access_key_id", "secret_access_key", "bucket")):
            return dict(c)
    except Exception:
        pass
    return None


def enabled():
    return _cfg() is not None


def _prefix(c):
    p = (c.get("prefix") or "quantum-lab/").strip("/")
    if not p:
        raise ValueError("prefix 가 비어 있으면 버킷 전체를 건드리게 되므로 허용하지 않습니다.")
    return p + "/"


def _client(c):
    import boto3
    return boto3.client("s3", endpoint_url=c.get("endpoint_url") or f"https://{c['account_id']}.r2.cloudflarestorage.com",
                        aws_access_key_id=c["access_key_id"], aws_secret_access_key=c["secret_access_key"],
                        region_name="auto")


def _key(c, rel):
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise ValueError("잘못된 경로")
    return _prefix(c) + rel


def push(rel):
    """data/<rel> 파일을 R2 로 업로드. 실패해도 앱은 계속 동작(오류는 status 에 기록)."""
    c = _cfg()
    if not c:
        return False
    try:
        with open(os.path.join(DATA_DIR, rel), "rb") as f:
            _client(c).put_object(Bucket=c["bucket"], Key=_key(c, rel), Body=f.read())
        return True
    except Exception as e:
        _state["error"] = f"업로드 실패({rel}): {e}"
        return False


def remove(rel):
    c = _cfg()
    if not c:
        return False
    try:
        _client(c).delete_object(Bucket=c["bucket"], Key=_key(c, rel))
        return True
    except Exception as e:
        _state["error"] = f"삭제 실패({rel}): {e}"
        return False


def list_keys():
    c = _cfg()
    if not c:
        return []
    cl, out, tok = _client(c), [], None
    pre = _prefix(c)
    while True:
        kw = dict(Bucket=c["bucket"], Prefix=pre)
        if tok:
            kw["ContinuationToken"] = tok
        r = cl.list_objects_v2(**kw)
        out += [(o["Key"][len(pre):], o["Size"]) for o in r.get("Contents", []) if not o["Key"].endswith("/")]
        if not r.get("IsTruncated"):
            return out
        tok = r["NextContinuationToken"]


def push_all(force=False):
    """로컬 data/ 의 모든 파일을 R2 로 올린다. force=False 면 R2 에 없는 파일만. 올린 개수 반환."""
    c = _cfg()
    if not c or not os.path.isdir(DATA_DIR):
        return 0
    have = set() if force else {k for k, _ in list_keys()}
    n = 0
    for root, _, files in os.walk(DATA_DIR):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), DATA_DIR).replace("\\", "/")
            if rel.startswith("_") or rel in have:
                continue
            n += 1 if push(rel) else 0
    return n


def db_rows(path):
    """sqlite 파일 안의 모든 표의 행 수 합계. 파일이 없거나 깨졌으면 0."""
    import sqlite3
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    try:
        con = sqlite3.connect(path)
        try:
            tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return sum(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tabs)
        finally:
            con.close()
    except Exception:
        return 0


def _download(cl, c, rel, dst):
    """임시 파일로 받은 뒤 교체 → 도중에 끊겨도 반쪽짜리 파일이 남지 않는다."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    body = cl.get_object(Bucket=c["bucket"], Key=_key(c, rel))["Body"].read()
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, dst)


def restore_once(force=False):
    """앱 시작 시 R2 에서 복원.
    · 로컬에 없는 파일은 내려받는다.
    · 로컬 .db 가 '비어 있는데' R2 쪽 .db 가 더 크면(= 재배포로 새로 만들어진 빈 DB) R2 것으로 교체한다.
      (예전 코드는 '파일이 존재하기만 하면' 건너뛰어서, 빈 DB 가 먼저 생기면 영영 복원되지 않았다)
    · 성공했을 때만 '완료'로 기록 → R2 가 잠깐 실패하거나 Secrets 가 늦게 잡혀도 다음 실행에서 다시 시도한다.
    · force=True 면 R2 의 모든 파일로 로컬을 덮어쓴다(수동 복구용)."""
    if _state["restored"] and not force:
        return 0
    c = _cfg()
    if not c:
        return 0                          # 설정이 없으면 '완료'로 치지 않는다 → 설정이 잡히면 다음 실행에서 시도
    n = 0
    try:
        cl = _client(c)
        for rel, size in list_keys():
            if rel.startswith("_") or rel.endswith(".part"):
                continue
            dst = os.path.join(DATA_DIR, rel)
            need = force or not os.path.exists(dst)
            if not need and rel.endswith(".db") and db_rows(dst) == 0 and size > os.path.getsize(dst):
                need = True               # 빈 로컬 DB → R2 의 실제 DB 로 교체
            if need:
                _download(cl, c, rel, dst)
                n += 1
        _state["restored"] = True
        _state["error"] = ""
        push_all(force=False)             # 로컬에만 있는 파일(연결 전에 올린 데이터)도 R2 로 올려 둔다
    except Exception as e:
        _state["error"] = f"복원 실패(다음 실행 때 다시 시도): {e}"
    return n


def restore_all():
    """수동 복구: R2 의 모든 파일로 로컬을 덮어쓴다. 내려받은 파일 수 반환."""
    return restore_once(force=True)


def status():
    """(연결여부, 메시지) — 쓰기/읽기/삭제 왕복 테스트"""
    c = _cfg()
    if not c:
        return False, "R2 미설정 → 로컬 디스크만 사용 중 (재시작 시 사라질 수 있음)"
    try:
        cl, k = _client(c), _key(c, "_healthcheck.txt")
        cl.put_object(Bucket=c["bucket"], Key=k, Body=b"ok")
        ok = cl.get_object(Bucket=c["bucket"], Key=k)["Body"].read() == b"ok"
        cl.delete_object(Bucket=c["bucket"], Key=k)
        n = len(list_keys())
        return ok, f"R2 연결 정상 · 버킷 {c['bucket']} · 폴더 {_prefix(c)} · 저장된 파일 {n}개" + (f" · 최근 오류: {_state['error']}" if _state["error"] else "")
    except Exception as e:
        return False, f"R2 연결 실패: {e}"
