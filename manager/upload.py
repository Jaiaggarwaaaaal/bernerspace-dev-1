import os
import argparse
from google.cloud import storage
from google.auth import exceptions as auth_exceptions
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def upload_to_gcs(file_path, bucket_name, project_id=None):
    """
    Uploads a file to a Google Cloud Storage bucket.

    :param file_path: Path to the file to upload.
    :param bucket_name: The name of the GCS bucket.
    :param project_id: A unique ID for the project to create a structured path.
    :return: True if the file was uploaded successfully, else False.
    """
    # --- Initialize Client ---
    try:
        storage_client = storage.Client()
    except auth_exceptions.DefaultCredentialsError:
        print("Error: Google Cloud credentials not found.")
        print("Please ensure GOOGLE_APPLICATION_CREDENTIALS is set correctly in your .env file.")
        return False

    # --- Validate Inputs ---
    if not os.path.exists(file_path):
        print(f"Error: The file '{file_path}' does not exist.")
        return False

    # --- Construct Blob Name ---
    # If a project ID is given, use a structured path. Otherwise, use a default.
    if project_id:
        # A simple versioning scheme based on timestamp for uniqueness
        version = f"v{int(time.time())}"
        blob_name = f"{project_id}/{version}/{os.path.basename(file_path)}"
    else:
        blob_name = f"unassigned/{os.path.basename(file_path)}"

    print(f"Uploading '{file_path}' to bucket '{bucket_name}' as '{blob_name}'...")

    # --- Perform Upload ---
    try:
        bucket = storage_client.bucket(bucket_name)
        if not bucket.exists():
            print(f"Error: The bucket '{bucket_name}' does not exist or you don't have access to it.")
            return False

        blob = bucket.blob(blob_name)
        blob.upload_from_filename(file_path)

        print(f"Successfully uploaded '{blob_name}' to '{bucket_name}'.")
        return True
    except Exception as e:
        print(f"An unexpected error occurred during upload: {e}")
        return False

def main():
    """Main function to parse arguments and initiate the upload."""
    parser = argparse.ArgumentParser(description="Upload a .tar.gz build context to Google Cloud Storage.")
    parser.add_argument("file_path", type=str, help="The path to the .tar.gz file to upload.")
    parser.add_argument("--project-id", type=str, help="Optional: A unique ID for the project for structured logging.", default=None)
    
    args = parser.parse_args()

    bucket_name = os.getenv('GCS_BUCKET_NAME')

    if not bucket_name:
        print("Error: GCS_BUCKET_NAME is not set in the .env file.")
        return

    upload_to_gcs(args.file_path, bucket_name, args.project_id)

if __name__ == "__main__":
    import time
    main()
