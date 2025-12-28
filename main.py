#!/usr/bin/python

import json
import os
import urllib2
import uuid

from flask import Flask
from flask import request
from flask import make_response

from google.appengine.ext import blobstore
from google.appengine.ext import ndb
from google.appengine.api import images

import cloudstorage as gcs


class UploadedImage(ndb.Model):
	"""NDB entity to store uploaded image metadata."""
	filename = ndb.StringProperty(required=True)
	folder = ndb.StringProperty(required=True)
	bucket = ndb.StringProperty(required=True)
	gcs_path = ndb.StringProperty(required=True)
	serving_url = ndb.StringProperty(required=True)
	source_url = ndb.StringProperty()  # Original URL the image was fetched from
	content_type = ndb.StringProperty()
	created_at = ndb.DateTimeProperty(auto_now_add=True)
	updated_at = ndb.DateTimeProperty(auto_now=True)

JSON_MIME_TYPE = 'application/json'

# Default bucket for uploads - set via environment variable or app.yaml
DEFAULT_BUCKET = os.environ.get('DEFAULT_BUCKET', 'nle3-images')
API_KEY = os.environ.get('API_KEY', '')

app = Flask(__name__)

@app.route('/image-url', methods=['GET'])
def image_url():
	bucket = request.args.get('bucket')
	image = request.args.get('image')

	if not all([bucket, image]):
		error = json.dumps({'error': 'Missing `bucket` or `image` parameter.'})
		return json_response(error, 422)

	filepath = (bucket + "/" + image)

	try:
		servingImage = images.get_serving_url(None, filename='/gs/' + filepath)
	except images.AccessDeniedError:
		error = json.dumps({'error': 'Ensure the GAE service account has access to the object in Google Cloud Storage.'})
		return json_response(error, 401)
	except images.ObjectNotFoundError:
		error = json.dumps({'error': 'The object was not found.'})
		return json_response(error, 404)
	except images.TransformationError:
		# A TransformationError may happen in several scenarios - if
		# the file is simply too large for the images service to
		# handle, if the image service doesn't have access to the file,
		# or if the file was already uploaded to the image service by
		# another App Engine app. For the latter case, we can try to
		# work around that by copying the file and re-uploading it to
		# the image service.
		error = json.dumps({'error': 'There was a problem transforming the image. Ensure the GAE service account has access to the object in Google Cloud Storage.'})
		return json_response(error, 400)

	return json_response(json.dumps({'image_url': servingImage}))


@app.route('/upload', methods=['POST'])
def upload_image():
	# Validate API key
	api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
	
	if not API_KEY:
		error = json.dumps({'error': 'API_KEY environment variable not configured on server.'})
		return json_response(error, 500)
	
	if not api_key or api_key != API_KEY:
		error = json.dumps({'error': 'Invalid or missing API key.'})
		return json_response(error, 401)
	
	# Get parameters from JSON body or form data
	if request.is_json:
		data = request.get_json()
		folder = data.get('folder') or data.get('project')
		image_url = data.get('image_url') or data.get('url')
	else:
		folder = request.form.get('folder') or request.form.get('project')
		image_url = request.form.get('image_url') or request.form.get('url')
	
	if not folder:
		error = json.dumps({'error': 'Missing `folder` or `project` parameter.'})
		return json_response(error, 422)
	
	if not image_url:
		error = json.dumps({'error': 'Missing `image_url` or `url` parameter.'})
		return json_response(error, 422)
	
	# Always generate a unique UUID filename
	# Extract extension from URL or default to .jpg
	ext = '.jpg'
	if '.' in image_url.split('/')[-1].split('?')[0]:
		ext = '.' + image_url.split('/')[-1].split('?')[0].split('.')[-1]
	filename = str(uuid.uuid4()) + ext
	
	# Download the image from the URL
	try:
		response = urllib2.urlopen(image_url, timeout=30)
		image_data = response.read()
		content_type = response.headers.get('Content-Type', 'image/jpeg')
	except urllib2.URLError as e:
		error = json.dumps({'error': 'Failed to download image from URL: ' + str(e)})
		return json_response(error, 400)
	except Exception as e:
		error = json.dumps({'error': 'Error downloading image: ' + str(e)})
		return json_response(error, 400)
	
	# Construct the GCS path
	gcs_path = '/' + DEFAULT_BUCKET + '/' + folder + '/' + filename
	
	# Upload to GCS
	try:
		gcs_file = gcs.open(gcs_path, 'w', content_type=content_type)
		gcs_file.write(image_data)
		gcs_file.close()
	except Exception as e:
		error = json.dumps({'error': 'Failed to upload to GCS: ' + str(e)})
		return json_response(error, 500)
	
	# Get the serving URL
	filepath = DEFAULT_BUCKET + '/' + folder + '/' + filename
	try:
		serving_url = images.get_serving_url(None, filename='/gs/' + filepath)
	except images.AccessDeniedError:
		error = json.dumps({'error': 'Uploaded but failed to get serving URL. Ensure the GAE service account has access.'})
		return json_response(error, 401)
	except images.ObjectNotFoundError:
		error = json.dumps({'error': 'Uploaded but object not found when getting serving URL.'})
		return json_response(error, 404)
	except images.TransformationError:
		error = json.dumps({'error': 'Uploaded but there was a problem transforming the image for serving.'})
		return json_response(error, 400)
	
	# Save to NDB
	uploaded_image = UploadedImage(
		filename=filename,
		folder=folder,
		bucket=DEFAULT_BUCKET,
		gcs_path='gs://' + filepath,
		serving_url=serving_url,
		source_url=image_url,
		content_type=content_type
	)
	entity_key = uploaded_image.put()
	
	return json_response(json.dumps({
		'id': entity_key.id(),
		'image_url': serving_url,
		'gcs_path': 'gs://' + filepath,
		'filename': filename
	}))


def json_response(data='', status=200, headers=None):
	headers = headers or {}
	if 'Content-Type' not in headers:
		headers['Content-Type'] = JSON_MIME_TYPE

	return make_response(data, status, headers)
