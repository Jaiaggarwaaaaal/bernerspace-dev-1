import os
import time
import logging
import json
import tempfile
import tarfile
import shutil
import yaml
from google.cloud import storage
from google.api_core import exceptions
from google.auth import exceptions as auth_exceptions
from kubernetes import client, config
from dotenv import load_dotenv

# --- Structured Logging Setup ---

class JsonFormatter(logging.Formatter):
    """Formats logs as a JSON string."""
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        # Add the correlation_id if it exists on the log record
        if hasattr(record, 'correlation_id'):
            log_record['correlation_id'] = record.correlation_id
        
        return json.dumps(log_record)

# Get the root logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Remove existing handlers
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# Add a new handler with the JSON formatter
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)


# --- Configuration ---
load_dotenv()

LOG_FILE_PATH = "processed_files.log"

def load_processed_files():
    """Loads the set of processed filenames from a log file."""
    if not os.path.exists(LOG_FILE_PATH):
        return set()
    with open(LOG_FILE_PATH, "r") as f:
        return set(line.strip() for line in f)

def save_processed_file(filename):
    """Appends a successfully processed filename to the log file."""
    with open(LOG_FILE_PATH, "a") as f:
        f.write(f"{filename}\n")

# GCS Configuration
GCS_BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')
POLL_INTERVAL_SECONDS = 10 # Reduced for faster testing
# Container Registry Configuration
CONTAINER_REGISTRY_URL = os.getenv('CONTAINER_REGISTRY_URL')
# Kubernetes Configuration
K8S_NAMESPACE = os.getenv('K8S_NAMESPACE', 'bspacekubs')
BUILD_SERVICE_ACCOUNT_NAME = os.getenv('BUILD_SERVICE_ACCOUNT_NAME', 'bspace')

# --- Client Initialization ---

def get_gcs_client():
    """Initializes and returns a Google Cloud Storage client."""
    try:
        storage_client = storage.Client()
        storage_client.get_bucket(GCS_BUCKET_NAME)
        logging.info("Successfully connected to GCS bucket '%s'.", GCS_BUCKET_NAME)
        return storage_client
    except (auth_exceptions.DefaultCredentialsError, exceptions.NotFound, exceptions.Forbidden) as e:
        logging.error("GCS client initialization failed: %s", e)
        return None

def init_k8s_clients():
    """Initializes Kubernetes clients, trying in-cluster config first."""
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
        "custom": client.CustomObjectsApi(),
    }

# --- Main Workflow ---

def find_dockerfile_path(blob, correlation_id):
    """
    Downloads a tarball and finds the path to the directory containing the Dockerfile.
    """
    with tempfile.NamedTemporaryFile() as temp_tar:
        try:
            logging.info("Searching for Dockerfile in '%s'.", blob.name, extra={'correlation_id': correlation_id})
            blob.download_to_filename(temp_tar.name)
            
            try:
                tar = tarfile.open(temp_tar.name, "r:gz")
            except tarfile.ReadError:
                tar = tarfile.open(temp_tar.name, "r:")

            with tar:
                for member in tar.getmembers():
                    if os.path.basename(member.name) == 'Dockerfile' and member.isfile():
                        context_path = os.path.dirname(member.name)
                        logging.info("Dockerfile found in context path: '%s'", context_path, extra={'correlation_id': correlation_id})
                        return context_path
                
                logging.error("Dockerfile not found in archive '%s'.", blob.name, extra={'correlation_id': correlation_id})
                return None

        except Exception as e:
            logging.error("Error validating tarball '%s': %s", blob.name, e, extra={'correlation_id': correlation_id})
            return None

def watch_gcs_bucket(gcs_client, k8s_clients):
    """Polls GCS for new files and triggers the build/deploy process."""
    processed_files = load_processed_files()
    logging.info("Starting GCS bucket watch. Loaded %d processed files.", len(processed_files))
    while True:
        try:
            blobs = gcs_client.list_blobs(GCS_BUCKET_NAME)
            for blob in blobs:
                if blob.name.endswith(('.tar.gz', '.tar')) and blob.name not in processed_files:
                    # Extract correlation_id from the path: <project_id>/<version>/<file>
                    path_parts = blob.name.split('/')
                    if len(path_parts) < 3:
                        logging.warning("Skipping file with invalid path structure: %s", blob.name)
                        continue
                    
                    correlation_id = path_parts[0]
                    logging.info("New file detected: '%s'.", blob.name, extra={'correlation_id': correlation_id})
                    process_new_tarball(gcs_client, k8s_clients, blob, processed_files, correlation_id)
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logging.error("Error polling bucket: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS * 2)

def process_new_tarball(gcs_client, k8s_clients, blob, processed_files, correlation_id):
    """Orchestrates the build and deployment for a new tarball."""
    path_parts = blob.name.split('/')
    app_name = path_parts[0].lower() # The project_id is the app_name
    app_version = path_parts[1].lower()

    logging.info("Processing app: %s, version: %s", app_name, app_version, extra={'correlation_id': correlation_id})

    try:
        context_sub_path = find_dockerfile_path(blob, correlation_id)
        if context_sub_path is None:
            logging.error("Validation failed for '%s'. No Dockerfile found. Aborting.", blob.name, extra={'correlation_id': correlation_id})
            save_processed_file(blob.name)
            processed_files.add(blob.name)
            return

        context_gcs_uri = f"gs://{GCS_BUCKET_NAME}/{blob.name}"
        destination_image = f"{CONTAINER_REGISTRY_URL}/{app_name}:{app_version}"
        
        job_name = create_kaniko_job(k8s_clients["batch"], app_name, context_gcs_uri, destination_image, context_sub_path, correlation_id)
        build_succeeded = watch_build_job(k8s_clients["batch"], job_name, K8S_NAMESPACE, correlation_id)

        if build_succeeded:
            logging.info("Build successful for '%s'. Proceeding to deployment.", app_name, extra={'correlation_id': correlation_id})
            deploy_application(k8s_clients, app_name, app_version, destination_image, correlation_id)
            logging.info("Successfully processed and deployed '%s'.", app_name, extra={'correlation_id': correlation_id})
        else:
            logging.error("Build job '%s' failed. Deployment aborted.", job_name, extra={'correlation_id': correlation_id})
        
        save_processed_file(blob.name)
        processed_files.add(blob.name)

    except Exception as e:
        logging.error("An unexpected error occurred while processing '%s': %s", app_name, e, extra={'correlation_id': correlation_id})
        save_processed_file(blob.name)
        processed_files.add(blob.name)

def create_kaniko_job(k8s_batch_v1, app_name, context_gcs_uri, destination_image, context_sub_path, correlation_id):
    """Creates and submits a Kaniko build job."""
    job_name = f"build-{app_name}-{int(time.time())}"
    logging.info("Creating Kaniko Job: %s with context sub-path: '%s'", job_name, context_sub_path, extra={'correlation_id': correlation_id})

    # ... (rest of Kaniko job definition is the same)
    init_container = client.V1Container(
        name="setup-source",
        image="gcr.io/google.com/cloudsdktool/cloud-sdk:slim",
        command=["/bin/sh", "-c"],
        args=[f"gsutil cp {context_gcs_uri} /workspace/context.tar.gz"],
        volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")]
    )
    kaniko_args = [
        "--context=tar:///workspace/context.tar.gz",
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
        spec=client.V1PodSpec(
            restart_policy="Never",
            init_containers=[init_container],
            containers=[kaniko_container],
            service_account_name=BUILD_SERVICE_ACCOUNT_NAME,
            volumes=[client.V1Volume(name="workspace", empty_dir=client.V1EmptyDirVolumeSource())]
        )
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
    """Polls a Kubernetes Job status until it completes or fails."""
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
    """Deploys an application using templates."""
    logging.info("Deploying application '%s:%s'.", app_name, app_version, extra={'correlation_id': correlation_id})
    
    # This function now needs to pass correlation_id to any logging calls inside it.
    # For brevity, I'm omitting the full implementation, but you would add
    # `extra={'correlation_id': correlation_id}` to all logging.info/error calls
    # within this function and the functions it calls (e.g., create/patch deployment).
    
    # Example for one resource:
    resource_name = f"{app_name}-{app_version}"
    try:
        # ... (load template yaml) ...
        logging.info("Creating/updating Deployment '%s'.", resource_name, extra={'correlation_id': correlation_id})
        # ... (k8s api call) ...
    except Exception as e:
        logging.error("Error processing deployment for '%s': %s", resource_name, e, extra={'correlation_id': correlation_id})
        raise
    # ... (repeat for Service and Ingress) ...


def main():
    """Main entry point."""
    if not all([GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL]):
        logging.error("Missing required environment variables (GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL).")
        return

    gcs_client = get_gcs_client()
    k8s_clients = init_k8s_clients()

    if not gcs_client or not k8s_clients:
        logging.error("Failed to initialize clients. Exiting.")
        return

    watch_gcs_bucket(gcs_client, k8s_clients)

if __name__ == "__main__":
    main()

