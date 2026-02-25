from prod.a2a_server import A2AServer
from prod.models import Task, Artifact
from kubernetes import client
import boto3
import base64
import tempfile
import os
import re
import botocore
import urllib.parse
import botocore.session
from botocore.signers import RequestSigner

DEFAULT_REGION = os.getenv("AWS_REGION", "us-east-1")
CROSS_ACCOUNT_ROLE_NAME = os.getenv(
    "CROSS_ACCOUNT_ROLE_NAME",
    "OncallAgentCrossAccountRole"
)


# ============================================
# Helper: Generate EKS Authentication Token
# ============================================



def generate_eks_token(cluster_name, region, creds):

    session = botocore.session.get_session()

    service_id = session.get_service_model('sts').service_id

    signer = RequestSigner(
        service_id,
        region,
        'sts',
        'v4',
        botocore.credentials.Credentials(
            creds["AccessKeyId"],
            creds["SecretAccessKey"],
            creds["SessionToken"]
        ),
        session.get_component('event_emitter')
    )

    params = {
        'method': 'GET',
        'url': f'https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15',
        'body': {},
        'headers': {
            'x-k8s-aws-id': cluster_name
        },
        'context': {}
    }

    signed_url = signer.generate_presigned_url(
        params,
        region_name=region,
        expires_in=60,
        operation_name=''
    )

    token = base64.urlsafe_b64encode(
        signed_url.encode('utf-8')
    ).decode('utf-8').rstrip('=')

    return f'k8s-aws-v1.{token}'


# ============================================
# Helper: Build Kubernetes Client
# ============================================

def get_k8s_client(cluster_name: str, account_id: str):

    role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"

    # 1️⃣ Assume Role
    sts = boto3.client("sts")

    assumed = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="oncall-session"
    )

    creds = assumed["Credentials"]

    # 2️⃣ Create EKS client
    eks = boto3.client(
        "eks",
        region_name=DEFAULT_REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"]
    )

    cluster_info = eks.describe_cluster(name=cluster_name)

    cluster_endpoint = cluster_info["cluster"]["endpoint"]
    cluster_ca = cluster_info["cluster"]["certificateAuthority"]["data"]

    # 3️⃣ Generate EKS token
    token = generate_eks_token(cluster_name, DEFAULT_REGION, creds)

    # 4️⃣ Configure Kubernetes client
    configuration = client.Configuration()
    configuration.host = cluster_endpoint
    configuration.verify_ssl = True
    configuration.api_key = {"authorization": "Bearer " + token}

    decoded_ca = base64.b64decode(cluster_ca)

    ca_file = tempfile.NamedTemporaryFile(delete=False)
    ca_file.write(decoded_ca)
    ca_file.close()

    configuration.ssl_ca_cert = ca_file.name

    return client.CoreV1Api(client.ApiClient(configuration))


# ============================================
# EKS A2A Agent
# ============================================

class EksAgent(A2AServer):

    async def process_task(self, task: Task) -> Task:

        try:
            metadata = task.metadata or {}

            cluster = metadata.get("cluster")
            namespace = metadata.get("namespace")
            pod = metadata.get("pod")
            account_id = metadata.get("aws_accid")

            if not cluster or not namespace or not pod or not account_id:
                task.mark_failed("Missing required metadata (cluster, namespace, pod, aws_accid)")
                return task

            # Build k8s client dynamically
            k8s = get_k8s_client(cluster, account_id)

            question = task.message.get_text().lower()

            # =====================================
            # LOGS
            # =====================================
            if "log" in question:

                logs = k8s.read_namespaced_pod_log(
                    name=pod,
                    namespace=namespace,
                    tail_lines=50
                )

                task.add_artifact(
                    Artifact.text(f"Last 50 log lines:\n{logs}")
                )

            # =====================================
            # STATUS / CRASH
            # =====================================
            else:

                pod_obj = k8s.read_namespaced_pod(pod, namespace)
                container = pod_obj.status.container_statuses[0]

                if container.state.waiting:
                    reason = container.state.waiting.reason
                    message = container.state.waiting.message or ""
                elif container.state.terminated:
                    reason = container.state.terminated.reason
                    message = container.state.terminated.message or ""
                else:
                    reason = pod_obj.status.phase
                    message = "Pod running"

                task.add_artifact(
                    Artifact.text(
                        f"Pod: {pod}\n"
                        f"Namespace: {namespace}\n"
                        f"Status: {reason}\n"
                        f"Message: {message}"
                    )
                )

            task.mark_completed()
            return task

        except Exception as e:
            task.mark_failed(str(e))
            return task


# ============================================
# Start Agent
# ============================================

if __name__ == "__main__":

    agent = EksAgent(
        agent_card_path="prod/agent_card.json",
        host="0.0.0.0",
        port=8081,
    )

    agent.run()
