from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SRC=(ROOT/'agent.py').read_text(encoding='utf-8')

def test_qa_promotion_declares_chunked_receive_capability():
    assert '"qa_chunked_release_receive"' in SRC
    assert 'QA_REQUIRED_PROMOTION_CAPABILITIES={"qa_image_release_receive","qa_chunked_release_receive"' in SRC

def test_qa_promote_uses_chunked_protocol_not_single_giant_multipart():
    block=SRC[SRC.index('def _remote_agent_qa_upload'):SRC.index('def _remote_agent_qa_deploy')]
    assert '/qa-release/chunk/start' in block
    assert '/complete' in block
    assert 'zip_path.read_bytes()' not in block
    assert 'f.read(QA_CHUNK_BYTES)' in block
    assert 'for attempt in range(1,4)' in block
    assert 'QA_CHUNK_OUT_OF_ORDER expected={index+1} got={index}' in block
    assert 'progress(sent,total,index)' in block

def test_target_has_start_chunk_complete_abort_routes():
    for route in (
        '/qa-release/chunk/start',
        '/qa-release/chunk/<upload_id>/<int:index>',
        '/qa-release/chunk/<upload_id>/complete',
        '/qa-release/chunk/<upload_id>/abort',
    ):
        assert route in SRC

def test_chunk_receiver_verifies_each_chunk_and_whole_artifact_sha():
    assert 'QA_CHUNK_SHA256_MISMATCH' in SRC
    assert 'QA_CHUNK_FINAL_SHA256_MISMATCH' in SRC
    assert '_stage_qa_release_zip(final' in SRC

def test_single_request_receiver_remains_for_backward_compatibility():
    assert '@app.post("/qa-release/upload")' in SRC
    assert 'def qa_release_upload()' in SRC
