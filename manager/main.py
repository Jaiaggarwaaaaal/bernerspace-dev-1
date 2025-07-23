import os
import time
import logging
import tempfile
import zipfile
import tarfile
import shutil
import yaml
from google.cloud import storage
from google.api_core import exceptions
from google.auth import exceptions as auth_exceptions
from kubernetes import client, config
from dotenv import load_dotenv

# ... (imports) ...
from dotenv import load_dotenv

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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
POLL_INTERVAL_SECONDS = 30
# Container Registry Configuration
CONTAINER_REGISTRY_URL = os.getenv('CONTAINER_REGISTRY_URL')
# Kubernetes Configuration
K8S_NAMESPACE = os.getenv('K8S_NAMESPACE', 'bspacekubs')
BUILD_SERVICE_ACCOUNT_NAME = os.getenv('BUILD_SERVICE_ACCOUNT_NAME', 'bspace')

processed_files = set()

# --- Client Initialization ---

def get_gcs_client():
    """Initializes and returns a Google Cloud Storage client."""
    try:
        storage_client = storage.Client()
        storage_client.get_bucket(GCS_BUCKET_NAME)
        logging.info(f"Successfully connected to GCS bucket '{GCS_BUCKET_NAME}'.")
        return storage_client
    except (auth_exceptions.DefaultCredentialsError, exceptions.NotFound, exceptions.Forbidden) as e:
        logging.error(f"GCS client initialization failed: {e}")
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
            logging.error(f"Could not configure Kubernetes client: {e}")
            return None
    return {
        "batch": client.BatchV1Api(),
        "apps": client.AppsV1Api(),
        "core": client.CoreV1Api(),
    }

# --- Main Workflow ---

def find_dockerfile_path(blob):
    """
    Downloads a tarball and finds the path to the directory containing the Dockerfile.

    Returns:
        str: The sub-path to the build context (e.g., '.') or None if not found.
    """
    with tempfile.NamedTemporaryFile() as temp_tar:
        try:
            logging.info(f"Searching for Dockerfile in '{blob.name}'...")
            blob.download_to_filename(temp_tar.name)
            with tarfile.open(temp_tar.name, "r:gz") as tar:
                members = tar.getmembers()
                
                # Search for Dockerfile at the root first
                root_dockerfile = any(
                    (m.name == 'Dockerfile' or m.name == './Dockerfile') and m.isfile()
                    for m in members
                )
                if root_dockerfile:
                    logging.info("Dockerfile found at the root.")
                    return "."

                # If not at root, search in immediate subdirectories
                for member in members:
                    # Path components: e.g., 'folder/Dockerfile' -> ['folder', 'Dockerfile']
                    path_parts = os.path.normpath(member.name).split(os.sep)
                    if len(path_parts) == 2 and path_parts[1] == 'Dockerfile' and member.isfile():
                        context_path = path_parts[0]
                        logging.info(f"Dockerfile found in subdirectory: '{context_path}'")
                        return context_path
                
                # If still not found, log and return None
                member_names = [member.name for member in members]
                logging.error(f"Dockerfile not found at root or in a subdirectory of '{blob.name}'.")
                logging.error(f"Archive contains the following files: {member_names}")
                return None

        except tarfile.ReadError:
            logging.error(f"File '{blob.name}' is not a valid tar.gz file.")
            return None
        except Exception as e:
            logging.error(f"Error validating tarball '{blob.name}': {e}")
            return None

def watch_gcs_bucket(gcs_client, k8s_clients):
    """Polls GCS for new .tar.gz files and triggers the build/deploy process."""
    processed_files = load_processed_files()
    logging.info(f"Loaded {len(processed_files)} previously processed files.")
    logging.info("Starting GCS bucket watch for .tar.gz files...")
    while True:
        try:
            blobs = gcs_client.list_blobs(GCS_BUCKET_NAME, prefix="uploads/")
            for blob in blobs:
                if blob.name.endswith('.tar.gz') and blob.name not in processed_files:
                    logging.info(f"New file detected: '{blob.name}'.")
                    process_new_tarball(gcs_client, k8s_clients, blob, processed_files) # Pass the set
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logging.error(f"Error polling bucket: {e}")
            time.sleep(POLL_INTERVAL_SECONDS * 2)

def process_new_tarball(gcs_client, k8s_clients, blob, processed_files):
    """Orchestrates the build and deployment for a new .tar.gz file."""
    # Extract app_name from the filename, e.g., "uploads/myapp.tar.gz" -> "myapp"
    app_name = os.path.basename(blob.name).replace('.tar.gz', '').lower()
    logging.info(f"Processing app: {app_name}")

    try:
        # 1. Find the Dockerfile build context path
        context_sub_path = find_dockerfile_path(blob)
        if not context_sub_path:
            logging.error(f"Validation failed for '{blob.name}'. No Dockerfile found. Aborting.")
            save_processed_file(blob.name) # Mark as processed to avoid retries
            processed_files.add(blob.name)
            return

        # 2. The uploaded tarball is the build context.
        context_gcs_uri = f"gs://{GCS_BUCKET_NAME}/{blob.name}"
        
        # 3. Create Kaniko Build Job
        destination_image = f"{CONTAINER_REGISTRY_URL}/{app_name}:latest"
        job_name = create_kaniko_job(k8s_clients["batch"], app_name, context_gcs_uri, destination_image, context_sub_path)

        # 4. Watch the build job for completion
        build_succeeded = watch_build_job(k8s_clients["batch"], job_name, K8S_NAMESPACE)

        # 5. Deploy on success
        if build_succeeded:
            logging.info(f"Build successful for '{app_name}'. Proceeding to deployment.")
            deploy_application(k8s_clients, app_name, destination_image)
            save_processed_file(blob.name)
            processed_files.add(blob.name)
            logging.info(f"Successfully processed and deployed '{app_name}'.")
        else:
            logging.error(f"Build job '{job_name}' failed. Deployment aborted.")
            save_processed_file(blob.name) # Also save on build failure
            processed_files.add(blob.name)

    except Exception as e:
        logging.error(f"An unexpected error occurred while processing '{app_name}': {e}")
        save_processed_file(blob.name) # Mark as processed on any failure
        processed_files.add(blob.name)


# --- Helper Functions ---

def get_template_path(template_name):
    """Constructs an absolute path to a template file."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(script_dir, 'templates', template_name)




def create_kaniko_job(k8s_batch_v1, app_name, context_gcs_uri, destination_image, context_sub_path="."):
    """Creates and submits a Kaniko build job using an initContainer to fetch the context."""
    job_name = f"build-{app_name}-{int(time.time())}"
    logging.info(f"Creating Kaniko Job: {job_name} with context sub-path: '{context_sub_path}'")

    # This init container downloads the gzipped tarball from GCS and places it
    # into a shared volume that the main build container can access.
    init_container = client.V1Container(
        name="setup-source",
        image="gcr.io/google.com/cloudsdktool/cloud-sdk:slim",
        command=["/bin/sh", "-c"],
        args=[f"gsutil cp {context_gcs_uri} /workspace/context.tar.gz"],
        volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")]
    )

    # The main Kaniko container now builds from the local tarball in the shared volume.
    # The context path must be prefixed with 'tar://' to indicate it's a tarball.
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
        volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "500m", "memory": "1Gi"}
        ),
    )

    # The Pod template now includes the init container and the shared volume.
    template = client.V1PodTemplateSpec(
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
    logging.info(f"Successfully submitted Job '{job_name}'.")
    return job_name

def watch_build_job(k8s_batch_v1, job_name, namespace):
    """Polls a Kubernetes Job status until it completes or fails."""
    logging.info(f"Watching job '{job_name}'...")
    while True:
        try:
            job_status = k8s_batch_v1.read_namespaced_job_status(job_name, namespace)
            if job_status.status.succeeded:
                logging.info(f"Job '{job_name}' succeeded.")
                return True
            if job_status.status.failed:
                logging.error(f"Job '{job_name}' failed.")
                return False
        except client.ApiException as e:
            if e.status == 404:
                logging.error(f"Job '{job_name}' not found. It may have been deleted.")
            else:
                logging.error(f"API error watching job '{job_name}': {e}")
            return False
        time.sleep(15)

# ... (previous code) ...

# Add the new networking client
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
            logging.error(f"Could not configure Kubernetes client: {e}")
            return None
    return {
        "batch": client.BatchV1Api(),
        "apps": client.AppsV1Api(),
        "core": client.CoreV1Api(),
        "networking": client.NetworkingV1Api(), # <-- Add this client
    }

# ... (previous code) ...

def deploy_application(k8s_clients, app_name, app_version, image_name):
    """Deploys an application using templates from the 'templates' directory."""
    logging.info(f"Deploying application '{app_name}:{app_version}' with image '{image_name}'.")
    
    resource_name = f"{app_name}-{app_version}"

    # Process Deployment
    try:
        deployment_template_path = get_template_path("deployment.yaml")
        with open(deployment_template_path) as f:
            deployment_manifest_str = f.read().replace("{{APP_NAME}}", app_name)
            deployment_manifest_str = deployment_manifest_str.replace("{{APP_VERSION}}", app_version)
            deployment_manifest_str = deployment_manifest_str.replace("{{IMAGE_NAME}}", image_name)
            deployment_manifest = yaml.safe_load(deployment_manifest_str)
        
        try:
            k8s_clients["apps"].read_namespaced_deployment(name=resource_name, namespace=K8S_NAMESPACE)
            logging.info(f"Deployment '{resource_name}' already exists. Patching with new image.")
            k8s_clients["apps"].patch_namespaced_deployment(
                name=resource_name,
                namespace=K8S_NAMESPACE,
                body=deployment_manifest,
            )
        except client.ApiException as e:
            if e.status == 404:
                logging.info(f"Deployment for '{resource_name}' not found. Creating new one.")
                k8s_clients["apps"].create_namespaced_deployment(
                    namespace=K8S_NAMESPACE, body=deployment_manifest
                )
            else:
                raise
        logging.info(f"Deployment '{resource_name}' created/updated.")

    except Exception as e:
        logging.error(f"Error processing deployment for '{resource_name}': {e}")
        raise

    # Process Service
    try:
        service_template_path = get_template_path("service.yaml")
        with open(service_template_path) as f:
            service_manifest_str = f.read().replace("{{APP_NAME}}", app_name)
            service_manifest_str = service_manifest_str.replace("{{APP_VERSION}}", app_version)
            service_manifest = yaml.safe_load(service_manifest_str)
        
        try:
            k8s_clients["core"].read_namespaced_service(name=resource_name, namespace=K8S_NAMESPACE)
            logging.info(f"Service '{resource_name}' already exists. No changes needed.")
        except client.ApiException as e:
            if e.status == 404:
                logging.info(f"Service for '{resource_name}' not found. Creating new one.")
                k8s_clients["core"].create_namespaced_service(
                    namespace=K8S_NAMESPACE, body=service_manifest
                )
                logging.info(f"Service '{resource_name}' created.")
            else:
                raise
    except Exception as e:
        logging.error(f"Error processing service for '{resource_name}': {e}")
        raise

    # Process Ingress
    try:
        domain_name = os.getenv('DOMAIN_NAME')
        if not domain_name:
            logging.warning("DOMAIN_NAME environment variable not set. Skipping Ingress creation.")
            return

        ingress_template_path = get_template_path("ingress.yaml")
        with open(ingress_template_path) as f:
            ingress_manifest_str = f.read().replace("{{APP_NAME}}", app_name)
            ingress_manifest_str = ingress_manifest_str.replace("{{APP_VERSION}}", app_version)
            ingress_manifest_str = ingress_manifest_str.replace("YOUR_DOMAIN.COM", domain_name)
            ingress_manifest = yaml.safe_load(ingress_manifest_str)

        ingress_name = f"{app_name}-{app_version}-ingress"
        try:
            k8s_clients["networking"].read_namespaced_ingress(name=ingress_name, namespace=K8S_NAMESPACE)
            logging.info(f"Ingress for '{resource_name}' already exists. Patching.")
            k8s_clients["networking"].patch_namespaced_ingress(
                name=ingress_name,
                namespace=K8S_NAMESPACE,
                body=ingress_manifest
            )
        except client.ApiException as e:
            if e.status == 404:
                logging.info(f"Ingress for '{resource_name}' not found. Creating new one.")
                k8s_clients["networking"].create_namespaced_ingress(
                    namespace=K8S_NAMESPACE, body=ingress_manifest
                )
            else:
                raise
        logging.info(f"Ingress for '{resource_name}' created/updated.")

    except Exception as e:
        logging.error(f"Error processing ingress for '{resource_name}': {e}")
        raise

# ... (rest of the code) ...



def main():
    """Main entry point."""
    if not all([GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL, os.getenv('LETSENCRYPT_EMAIL')]):
        logging.error("Missing required environment variables (GCS_BUCKET_NAME, CONTAINER_REGISTRY_URL, LETSENCRYPT_EMAIL).")
        return

    gcs_client = get_gcs_client()
    k8s_clients = init_k8s_clients()

    if not gcs_client or not k8s_clients:
        logging.error("Failed to initialize clients. Exiting.")
        return

    setup_cluster_issuer(k8s_clients)
    watch_gcs_bucket(gcs_client, k8s_clients)

def setup_cluster_issuer(k8s_clients):
    """Creates or updates the ClusterIssuer."""
    email = os.getenv('LETSENCRYPT_EMAIL')
    if not email:
        logging.warning("LETSENCRYPT_EMAIL environment variable not set. Skipping ClusterIssuer setup.")
        return

    try:
        template_path = get_template_path("../cluster-issuer.yaml")
        with open(template_path) as f:
            manifest_str = f.read().replace("{{LETSENCRYPT_EMAIL}}", email)
            manifest = yaml.safe_load(manifest_str)

        # The CustomObjectsApi is needed for ClusterIssuer
        custom_api = client.CustomObjectsApi(k8s_clients['apps'].api_client)
        
        try:
            custom_api.get_cluster_custom_object(
                group="cert-manager.io",
                version="v1",
                plural="clusterissuers",
                name=manifest['metadata']['name']
            )
            logging.info("ClusterIssuer already exists. Patching.")
            custom_api.patch_cluster_custom_object(
                group="cert-manager.io",
                version="v1",
                plural="clusterissuers",
                name=manifest['metadata']['name'],
                body=manifest
            )
        except client.ApiException as e:
            if e.status == 404:
                logging.info("ClusterIssuer not found. Creating new one.")
                custom_api.create_cluster_custom_object(
                    group="cert-manager.io",
                    version="v1",
                    plural="clusterissuers",
                    body=manifest
                )
            else:
                raise
        logging.info("ClusterIssuer created/updated.")

    except Exception as e:
        logging.error(f"Error setting up ClusterIssuer: {e}")
        raise


if __name__ == "__main__":
    main()
