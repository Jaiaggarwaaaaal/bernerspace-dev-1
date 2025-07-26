import os
import time
import logging
import json
import tempfile
import tarfile
import re
import yaml
from google.cloud import storage
from google.api_core import exceptions
from google.auth import exceptions as auth_exceptions
from kubernetes import client, config
from dotenv import load_dotenv

# --- Structured Logging Setup ---
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, 'correlation_id'):
            log_record['correlation_id'] = record.correlation_id
        return json.dumps(log_record)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)

# --- Configuration ---
load_dotenv()
LOG_FILE_PATH = "processed_files.log"
GCS_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
POLL_INTERVAL_SECONDS = 10
CONTAINER_REGISTRY_URL = os.getenv('CONTAINER_REGISTRY_URL')
K8S_NAMESPACE = os.getenv('K8S_NAMESPACE', 'bspacekubs')
BUILD_SERVICE_ACCOUNT_NAME = os.getenv('BUILD_SERVICE_ACCOUNT_NAME', 'bspace')
DOMAIN_NAME = os.getenv('DOMAIN_NAME')

def load_processed_files():
    if not os.path.exists(LOG_FILE_PATH): return set()
    with open(LOG_FILE_PATH, "r") as f: return set(line.strip() for line in f)

def save_processed_file(filename):
    with open(LOG_FILE_PATH, "a") as f: f.write(f"{filename}\n")

def get_gcs_client():
    try:
        storage_client = storage.Client()
        storage_client.get_bucket(GCS_BUCKET_NAME)
        logging.info("Successfully connected to GCS bucket '%s'.", GCS_BUCKET_NAME)
        return storage_client
    except Exception as e:
        logging.error("GCS client initialization failed: %s", e)
        return None

def init_k8s_clients():
    try:
        config.load_incluster_config()
        logging.info("Loaded in-cluster Kubernetes configuration.")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logging.info("Loaded local kubeconfig for development.")
        except config.ConfigException as e:
            logging.error("Could not configure Kubernetes client: %s", e)
            return None
    return {
        "batch": client.BatchV1Api(),
        "apps": client.AppsV1Api(),
        "core": client.CoreV1Api(),
        "networking": client.NetworkingV1Api(),
    }

def sanitize_k8s_name(name):
    name = name.lower()
    name = re.sub(r'[^a-z0-9-]', '-', name)
    return name.strip('-')

def find_dockerfile_path(blob, correlation_id):
    with tempfile.NamedTemporaryFile() as temp_tar:
        try:
            logging.info("Searching for Dockerfile in '%s'.", blob.name, extra={'correlation_id': correlation_id})
            blob.download_to_filename(temp_tar.name)
            open_mode = "r:gz" if blob.name.endswith(".tar.gz") else "r:"
            with tarfile.open(temp_tar.name, open_mode) as tar:
                for member in tar.getmembers():
                    if os.path.basename(member.name).lower() == 'dockerfile' and member.isfile():
                        context_path = os.path.dirname(member.name)
                        logging.info("Dockerfile found in context path: '%s'", context_path, extra={'correlation_id': correlation_id})
                        return context_path
            logging.error("Dockerfile not found in archive '%s'.", blob.name, extra={'correlation_id': correlation_id})
            return None
        except Exception as e:
            logging.error("Error validating tarball '%s': %s", blob.name, e, extra={'correlation_id': correlation_id})
            return None

def create_kaniko_job(k8s_batch_v1, app_name, app_version, context_gcs_uri, destination_image, context_sub_path, correlation_id):
    job_name = sanitize_k8s_name(f"build-{app_name}-{app_version}-{int(time.time())}")
    logging.info("Creating Kaniko Job: %s", job_name, extra={'correlation_id': correlation_id})
    # ... (rest of Kaniko job creation is the same)
    init_container = client.V1Container(
        name="setup-source",
        image="gcr.io/google.com/cloudsdktool/cloud-sdk:slim",
        command=["/bin/sh", "-c"],
        args=[f"gsutil cp {context_gcs_uri} /workspace/context.tar.gz"],
        volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")]
    )
    kaniko_args = [
        f"--context=tar:///workspace/context.tar.gz",
        f"--destination={destination_image}",
        "--cache=true",
        f"--cache-repo={CONTAINER_REGISTRY_URL}/cache",
    ]
    if context_sub_path and context_sub_path != ".":
        kaniko_args.append(f"--context-sub-path={context_sub_path}")
    kaniko_container = client.V1Container(
        name="kaniko",
        image="gcr.io/kaniko-project/executor:v1.9.0",
        args=kaniko_args,
        volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")]
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": app_name, "correlation_id": correlation_id}),
        spec=client.V1PodSpec(
            restart_policy="Never",
            init_containers=[init_container],
            containers=[kaniko_container],
            service_account_name=BUILD_SERVICE_ACCOUNT_NAME,
            volumes=[client.V1Volume(name="workspace", empty_dir=client.V1EmptyDirVolumeSource())]
        ),
    )
    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=job_name, labels={"app": app_name}),
        spec=client.V1JobSpec(template=template, backoff_limit=1)
    )
    k8s_batch_v1.create_namespaced_job(body=job, namespace=K8S_NAMESPACE)
    logging.info("Successfully submitted Job '%s'.", job_name, extra={'correlation_id': correlation_id})
    return job_name

def watch_build_job(k8s_batch_v1, job_name, namespace, correlation_id):
    # ... (this function is unchanged)
    logging.info("Watching job '%s'...", job_name, extra={'correlation_id': correlation_id})
    while True:
        try:
            job_status = k8s_batch_v1.read_namespaced_job_status(job_name, namespace)
            if job_status.status.succeeded:
                logging.info("Job '%s' succeeded.", job_name, extra={'correlation_id': correlation_id})
                return True
            if job_status.status.failed:
                logging.error("Job '%s' failed.", job_name, extra={'correlation_id': correlation_id})
                return False
        except client.ApiException as e:
            logging.error("API error watching job '%s': %s", job_name, e, extra={'correlation_id': correlation_id})
            return False
        time.sleep(15)

def deploy_application(k8s_clients, app_name, app_version, image_name, correlation_id):
    logging.info("Deploying application '%s:%s'.", app_name, app_version, extra={'correlation_id': correlation_id})
    
    # A unique name for labels and selectors, consistent across versions of the same app
    base_name = sanitize_k8s_name(f"{app_name}-{correlation_id}")
    # A unique name for each versioned resource instance (Deployment, Service)
    resource_name = sanitize_k8s_name(f"{base_name}-{app_version}")

    def get_template(template_name):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        template_path = os.path.join(script_dir, 'templates', template_name)
        with open(template_path) as f:
            return f.read()
    
    # Process Deployment
    try:
        deployment_manifest_str = get_template("deployment.yaml")
        deployment_manifest_str = deployment_manifest_str.replace("{{APP_NAME}}", app_name)
        deployment_manifest_str = deployment_manifest_str.replace("{{BASE_NAME}}", base_name)
        deployment_manifest_str = deployment_manifest_str.replace("{{RESOURCE_NAME}}", resource_name)
        deployment_manifest_str = deployment_manifest_str.replace("{{APP_VERSION}}", app_version)
        deployment_manifest_str = deployment_manifest_str.replace("{{IMAGE_NAME}}", image_name)
        deployment_manifest = yaml.safe_load(deployment_manifest_str)
        
        try:
            k8s_clients["apps"].read_namespaced_deployment(name=resource_name, namespace=K8S_NAMESPACE)
            k8s_clients["apps"].patch_namespaced_deployment(name=resource_name, namespace=K8S_NAMESPACE, body=deployment_manifest)
            logging.info("Patched existing deployment '%s'.", resource_name, extra={'correlation_id': correlation_id})
        except client.ApiException as e:
            if e.status == 404:
                k8s_clients["apps"].create_namespaced_deployment(namespace=K8S_NAMESPACE, body=deployment_manifest)
                logging.info("Created new deployment '%s'.", resource_name, extra={'correlation_id': correlation_id})
            else: raise
    except Exception as e:
        logging.error("Error processing deployment for '%s': %s", resource_name, e, extra={'correlation_id': correlation_id})
        raise

    # Process Service
    try:
        service_manifest_str = get_template("service.yaml")
        service_manifest_str = service_manifest_str.replace("{{BASE_NAME}}", base_name)
        service_manifest_str = service_manifest_str.replace("{{RESOURCE_NAME}}", resource_name)
        service_manifest_str = service_manifest_str.replace("{{APP_VERSION}}", app_version)
        service_manifest = yaml.safe_load(service_manifest_str)
        
        try:
            k8s_clients["core"].read_namespaced_service(name=resource_name, namespace=K8S_NAMESPACE)
            logging.info("Service '%s' already exists, skipping patch.", resource_name, extra={'correlation_id': correlation_id})
        except client.ApiException as e:
            if e.status == 404:
                k8s_clients["core"].create_namespaced_service(namespace=K8S_NAMESPACE, body=service_manifest)
                logging.info("Created new service '%s'.", resource_name, extra={'correlation_id': correlation_id})
            else: raise
    except Exception as e:
        logging.error("Error processing service for '%s': %s", resource_name, e, extra={'correlation_id': correlation_id})
        raise

    # Process Ingress
    try:
        if not DOMAIN_NAME: return
        ingress_manifest_str = get_template("ingress.yaml")
        ingress_manifest_str = ingress_manifest_str.replace("{{APP_NAME}}", app_name)
        ingress_manifest_str = ingress_manifest_str.replace("{{BASE_NAME}}", base_name)
        ingress_manifest_str = ingress_manifest_str.replace("{{RESOURCE_NAME}}", resource_name)
        ingress_manifest_str = ingress_manifest_str.replace("{{DOMAIN_NAME}}", DOMAIN_NAME)
        ingress_manifest = yaml.safe_load(ingress_manifest_str)
        ingress_name = f"{resource_name}-ingress"

        try:
            k8s_clients["networking"].read_namespaced_ingress(name=ingress_name, namespace=K8S_NAMESPACE)
            k8s_clients["networking"].patch_namespaced_ingress(name=ingress_name, namespace=K8S_NAMESPACE, body=ingress_manifest)
            logging.info("Patched existing ingress '%s'.", ingress_name, extra={'correlation_id': correlation_id})
        except client.ApiException as e:
            if e.status == 404:
                k8s_clients["networking"].create_namespaced_ingress(namespace=K8S_NAMESPACE, body=ingress_manifest)
                logging.info("Created new ingress '%s'.", ingress_name, extra={'correlation_id': correlation_id})
            else: raise
        
        ingress_url = f"https://{app_name}.{DOMAIN_NAME}"
        logging.info("Application ingress URL: %s", ingress_url, extra={'correlation_id': correlation_id})

    except Exception as e:
        logging.error("Error processing ingress for '%s': %s", resource_name, e, extra={'correlation_id': correlation_id})
        raise

def process_new_tarball(gcs_client, k8s_clients, blob, processed_files):
    path_parts = blob.name.split('/')
    # CORRECTED PATH PARSING: <project_name>/<project_id>/<version>/...
    if len(path_parts) < 4:
        logging.warning("Skipping file with invalid path structure: %s", blob.name)
        return

    app_name = sanitize_k8s_name(path_parts[0])
    correlation_id = path_parts[1]
    app_version = sanitize_k8s_name(path_parts[2])

    logging.info("Processing app: %s, version: %s", app_name, app_version, extra={'correlation_id': correlation_id})

    try:
        context_sub_path = find_dockerfile_path(blob, correlation_id)
        if context_sub_path is None: raise ValueError("Dockerfile not found.")

        destination_image = f"{CONTAINER_REGISTRY_URL}/{app_name}:{app_version}"
        job_name = create_kaniko_job(k8s_clients["batch"], app_name, app_version, f"gs://{GCS_BUCKET_NAME}/{blob.name}", destination_image, context_sub_path, correlation_id)
        build_succeeded = watch_build_job(k8s_clients["batch"], job_name, K8S_NAMESPACE, correlation_id)

        if build_succeeded:
            deploy_application(k8s_clients, app_name, app_version, destination_image, correlation_id)
            logging.info("Successfully processed and deployed '%s'.", app_name, extra={'correlation_id': correlation_id})
        else:
            raise RuntimeError(f"Build job {job_name} failed.")

    except Exception as e:
        logging.error("An unexpected error occurred while processing '%s': %s", blob.name, e, extra={'correlation_id': correlation_id})
    finally:
        save_processed_file(blob.name)
        processed_files.add(blob.name)

def watch_gcs_bucket(gcs_client, k8s_clients):
    processed_files = load_processed_files()
    logging.info("Starting GCS bucket watch. Loaded %d processed files.", len(processed_files))
    while True:
        try:
            blobs = gcs_client.list_blobs(GCS_BUCKET_NAME)
            for blob in blobs:
                if blob.name.endswith(('.tar.gz', '.tar')) and blob.name not in processed_files:
                    process_new_tarball(gcs_client, k8s_clients, blob, processed_files)
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logging.error("Error polling bucket: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS * 2)

def main():
    if not all([GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL, DOMAIN_NAME]):
        logging.error("Missing required environment variables.")
        return
    gcs_client = get_gcs_client()
    k8s_clients = init_k8s_clients()
    if not gcs_client or not k8s_clients:
        logging.error("Failed to initialize clients. Exiting.")
        return
    watch_gcs_bucket(gcs_client, k8s_clients)

if __name__ == "__main__":
    main()
