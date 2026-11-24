from botocore.stub import Stubber

from tests.conftest import load_service


def test_retrieve_maps_results_to_citations():
    kb = load_service("mcp_server", "knowledge_base")
    import boto3

    client = boto3.client("bedrock-agent-runtime", region_name="us-east-1")
    stub = Stubber(client)
    stub.add_response(
        "retrieve",
        {
            "retrievalResults": [
                {
                    "content": {"text": "Refunds take 5 days."},
                    "location": {"type": "S3", "s3Location": {"uri": "s3://bucket/docs/refunds.md"}},
                    "score": 0.734567,
                },
            ]
        },
        {
            "knowledgeBaseId": "KBID123456",
            "retrievalQuery": {"text": "refund"},
            "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 3}},
        },
    )
    with stub:
        out = kb.KnowledgeBaseRetriever("KBID123456", client).retrieve("refund", 3)
    assert out == [
        {"citation_id": 1, "source": "refunds.md", "uri": "s3://bucket/docs/refunds.md", "score": 0.7346, "text": "Refunds take 5 days."}
    ]
