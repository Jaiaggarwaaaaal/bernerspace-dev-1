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
PROCESSED_FILES_CM = "processed-files-log"  # ConfigMap name
PROCESSED_FILES_KEY = "files"             # Key in the ConfigMap
GCS_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
POLL_INTERVAL_SECONDS = 10
CONTAINER_REGISTRY_URL = os.getenv('CONTAINER_REGISTRY_URL')
K8S_NAMESPACE = os.getenv('K8S_NAMESPACE', 'bspacekubs')
BUILD_SERVICE_ACCOUNT_NAME = os.getenv('BUILD_SERVICE_ACCOUNT_NAME', 'bspace')
DOMAIN_NAME = ideabrowse.com

k8s_core_v1 = None # Will be initialized in main

def load_processed_files():
    """Loads the list of processed files from a ConfigMap."""
    global k8s_core_v1
    try:
        cm = k8s_core_v1.read_namespaced_config_map(name=PROCESSED_FILES_CM, namespace=K8S_NAMESPACE)
        files_str = cm.data.get(PROCESSED_FILES_KEY, "")
        return set(files_str.splitlines())
    except client.ApiException as e:
        if e.status == 404:
            logging.warning("ConfigMap '%s' not found, starting with an empty set of processed files.", PROCESSED_FILES_CM)
            return set()
        else:
            logging.error("Failed to load ConfigMap '%s': %s", PROCESSED_FILES_CM, e)
            # In case of other API errors, exit or handle appropriately
            raise

def save_processed_file(filename):
    """Adds a filename to the processed files list in the ConfigMap."""
    global k8s_core_v1
    try:
        cm = k8s_core_v1.read_namespaced_config_map(name=PROCESSED_FILES_CM, namespace=K8S_NAMESPACE)
        files = cm.data.get(PROCESSED_FILES_KEY, "")
        if filename not in files.splitlines():
            cm.data[PROCESSED_FILES_KEY] = files + filename + "\n"
            k8s_core_v1.patch_namespaced_config_map(name=PROCESSED_FILES_CM, namespace=K8S_NAMESPACE, body=cm)
    except client.ApiException as e:
        if e.status == 404:
            # Create the ConfigMap if it doesn't exist
            body = client.V1ConfigMap(
                api_version="v1",
                kind="ConfigMap",
                metadata=client.V1ObjectMeta(name=PROCESSED_FILES_CM),
                data={PROCESSED_FILES_KEY: filename + "\n"}
            )
            k8s_core_v1.create_namespaced_config_map(namespace=K8S_NAMESPACE, body=body)
        else:
            logging.error("Failed to save to ConfigMap '%s': %s", PROCESSED_FILES_CM, e)
            raise

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
    logging.info("Watching job '%s'...", job_name, extra={'correlation_id': correlation_id})
    
    # Retry logic for finding the job initially
    max_retries = 3
    retry_delay = 5 # seconds
    for attempt in range(max_retries):
        try:
            job_status = k8s_batch_v1.read_namespaced_job_status(job_name, namespace)
            logging.info("Successfully found job '%s'. Proceeding to watch.", job_name, extra={'correlation_id': correlation_id})
            break # Exit retry loop if job is found
        except client.ApiException as e:
            if e.status == 404 and attempt < max_retries - 1:
                logging.warning("Job '%s' not found, retrying in %d seconds...", job_name, retry_delay, extra={'correlation_id': correlation_id})
                time.sleep(retry_delay)
            else:
                logging.error("API error finding job '%s' after %d attempts: %s", job_name, attempt + 1, e, extra={'correlation_id': correlation_id})
                return False
    
    # Main watch loop
    while True:
        try:
            job_status = k8s_batch_v1.read_namespaced_job_status(job_name, namespace)
            if job_status.status.succeeded:
                logging.info("Job '%s' succeeded.", job_name, extra={'correlation_id': correlation_id})
                return True
            if job_status.status.failed:
                logging.error("Job '%s' failed. Status: %s", job_name, job_status.status, extra={'correlation_id': correlation_id})
                return False
            
            # Provide more detailed status logging
            active = job_status.status.active
            logging.info("Job '%s' is still running. Active pods: %s", job_name, active, extra={'correlation_id': correlation_id})

        except client.ApiException as e:
            logging.error("API error watching job '%s': %s", job_name, e, extra={'correlation_id': correlation_id})
            return False
        time.sleep(15)


def deploy_application(k8s_clients, app_name, app_version, image_name, correlation_id):
    logging.info("Deploying application '%s:%s'.", app_name, app_version, extra={'correlation_id': correlation_id})
    
    base_name = sanitize_k8s_name(f"{app_name}-{correlation_id}")
    resource_name = sanitize_k8s_name(f"{base_name}-{app_version}")
    # Stable ingress name that doesn't change with versions
    ingress_name = sanitize_k8s_name(f"{base_name}-ingress")

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
        ingress_manifest_str = ingress_manifest_str.replace("{{INGRESS_NAME}}", ingress_name)
        ingress_manifest_str = ingress_manifest_str.replace("{{RESOURCE_NAME}}", resource_name)
        ingress_manifest_str = ingress_manifest_str.replace("{{DOMAIN_NAME}}", DOMAIN_NAME)
        ingress_manifest_str = ingress_manifest_str.replace("{{BASE_NAME}}", base_name)
        ingress_manifest = yaml.safe_load(ingress_manifest_str)


        ingress_name = f"{app_name}-{app_version}-ingress"
        try:
            k8s_clients["networking"].read_namespaced_ingress(name=ingress_name, namespace=K8S_NAMESPACE)

            k8s_clients["networking"].patch_namespaced_ingress(name=ingress_name, namespace=K8S_NAMESPACE, body=ingress_manifest)
            logging.info("Patched existing ingress '%s' to point to service '%s'.", ingress_name, resource_name, extra={'correlation_id': correlation_id})
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
            # Mark as processed only on full success
            save_processed_file(blob.name)
            processed_files.add(blob.name)
        else:
            raise RuntimeError(f"Build job {job_name} failed.")

    except Exception as e:
        logging.error("An unexpected error occurred while processing '%s': %s", blob.name, e, extra={'correlation_id': correlation_id})
        # Do not save to processed_files on failure, so it can be retried.


def watch_gcs_bucket(gcs_client, k8s_clients):
    processed_files = load_processed_files()
    logging.info("Starting GCS bucket watch. Loaded %d processed files.", len(processed_files))
    while True:
        try:
            all_blobs = list(gcs_client.list_blobs(GCS_BUCKET_NAME))
            
            # Sort blobs by creation time, newest first
            sorted_blobs = sorted(all_blobs, key=lambda b: b.time_created, reverse=True)
            
            for blob in sorted_blobs:
                if blob.name.endswith(('.tar.gz', '.tar')) and blob.name not in processed_files:
                    process_new_tarball(gcs_client, k8s_clients, blob, processed_files)
            
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logging.error("Error polling bucket: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS * 2)

def main():
    global k8s_core_v1
    if not all([GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL, DOMAIN_NAME]):
        logging.error("Missing required environment variables: GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL, DOMAIN_NAME")
        return
    
    k8s_clients = init_k8s_clients()
    if not k8s_clients:
        logging.error("Failed to initialize Kubernetes clients. Exiting.")
        return
    
    k8s_core_v1 = k8s_clients["core"]

    gcs_client = get_gcs_client()
    if not gcs_client:
        logging.error("Failed to initialize GCS client. Exiting.")
        return
        
    watch_gcs_bucket(gcs_client, k8s_clients)

if __name__ == "__main__":
    main()
