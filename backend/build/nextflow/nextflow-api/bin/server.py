#!/usr/bin/env python3

import base64
import bson
import json
import multiprocessing as mp
import os
import pandas as pd
import shutil
import socket
import subprocess
import time
import tornado
import tornado.escape
import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web

import backend
import env
# import model as Model
import visualizer as Visualizer
import workflow as Workflow



def list_dir_recursive(path, relpath_start=''):
	files = [os.path.join(dir, f) for (dir, subdirs, filenames) in os.walk(path) for f in filenames]
	files = [os.path.relpath(f, start=relpath_start) for f in files]
	files.sort()

	return files



def message(status, message):
	return {
		'status': status,
		'message': message
	}


#-------------------------------------
# DATASETS Classes
#-------------------------------------

class DatasetQueryHandler(tornado.web.RequestHandler):

	async def get(self):
		page = int(self.get_query_argument('page', 0))
		page_size = int(self.get_query_argument('page_size', 100))

		db = self.settings['db']
		datasets = await db.dataset_query(page, page_size)

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode(datasets))



class DatasetCreateHandler(tornado.web.RequestHandler):

	REQUIRED_KEYS = set([
		'experiment'
	])

	DEFAULTS = {
		'author': '',
		'description': '',
		'nfiles': 0
	}

	def get(self):
		dataset = {**self.DEFAULTS, **{ '_id': '0' }}

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode(dataset))

	async def post(self):

		db = self.settings['db']

		# make sure request body is valid
		try:
			data = tornado.escape.json_decode(self.request.body)
			missing_keys = self.REQUIRED_KEYS - data.keys()
		except json.JSONDecodeError:
			self.set_status(422)
			self.write(message(422, 'Ill-formatted JSON'))
			return

		if missing_keys:
			self.set_status(400)
			self.write(message(400, 'Missing required field(s): %s' % list(missing_keys)))
			return

		# create dataset
		dataset = {**self.DEFAULTS, **data}
		dataset['_id'] = str(bson.ObjectId())

		# append creation timestamp to dataset
		dataset['date_created'] = int(time.time() * 1000)

		# transform experiment name to lowercase
		dataset['experiment'] = dataset['experiment'].lower() 

		# save dataset
		await db.dataset_create(dataset)

		# create dataset directory
		dataset_dir = os.path.join(env.DATASETS_DIR, dataset['_id'])
		os.makedirs(dataset_dir)

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode({ '_id': dataset['_id'] }))



class DatasetEditHandler(tornado.web.RequestHandler):

	REQUIRED_KEYS = set([
		'experiment',
		'author',
		'description'
	])

	DEFAULTS = {
		'author': '',
		'description': '',
		'nfiles': 0
	}

	async def get(self, id):
		db = self.settings['db']

		try:
			# get dataset
			dataset = await db.dataset_get(id)

			# append list of input files
			dataset_dir = os.path.join(env.DATASETS_DIR, id)
			if os.path.exists(dataset_dir):
				dataset['files'] = list_dir_recursive(dataset_dir, relpath_start=dataset_dir)
			else:
				dataset['files'] = []

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(dataset))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to get dataset \"%s\"' % id))

	async def post(self, id):
		db = self.settings['db']

		# make sure request body is valid
		try:
			data = tornado.escape.json_decode(self.request.body)
			added_keys = data.keys() - self.REQUIRED_KEYS
		except json.JSONDecodeError:
			self.set_status(422)
			self.write(message(422, 'Ill-formatted JSON'))

		if added_keys:
			self.set_status(400)
			self.write(message(400, 'There are more field(s) than allowed: %s' % list(self.REQUIRED_KEYS)))
			return

		try:
			# update dataset from request body
			dataset = await db.dataset_get(id)
			dataset = {**self.DEFAULTS, **dataset, **data}

			# transform experiment name to lowercase
			dataset['experiment'] = dataset['experiment'].lower() 

			# save dataset
			await db.dataset_update(id, dataset)

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode({ '_id': id }))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to update dataset \"%s\"' % id))

	async def delete(self, id):
		db = self.settings['db']

		try:
			# delete dataset
			await db.dataset_delete(id)

			# delete dataset directory
			shutil.rmtree(os.path.join(env.DATASETS_DIR, id), ignore_errors=True)

			self.set_status(200)
			self.write(message(200, 'Dataset \"%s\" was deleted' % id))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to delete dataset \"%s\"' % id))




class DatasetUploadHandler(tornado.web.RequestHandler):

	async def post(self, id, format, parameter):
		db = self.settings['db']

		# make sure request body contains files
		files = self.request.files

		if not files:
			self.set_status(400)
			self.write(message(400, 'No files were uploaded'))
			return

		try:
			# get dataset
			dataset = await db.dataset_get(id)
		except:
			self.set_status(404)
			self.write(message(404, 'Dataset \"%s\" was not found' % id))

		# determine which type of upload to do
		if format == "directory-path":
			try:
				# initialize input directory using the parameter 'directory-path'
				input_dir = os.path.join(env.DATASETS_DIR, id, parameter)
				os.makedirs(input_dir, exist_ok=True)

				# save uploaded files to input directory
				filenames = []

				for f_list in files.values():
					for f_arg in f_list:
						filename, body = f_arg['filename'], f_arg['body']
						with open(os.path.join(input_dir, filename), 'wb') as f:
							f.write(body)
						filenames.append(filename)

				# increase nfiles
				dataset['nfiles'] += len(filenames)

				# save dataset
				await db.dataset_update(id, dataset)

				self.set_status(200)
				self.write(message(200, 'File \"%s\" was uploaded for dataset \"%s\" successfully' % (filenames, id)))
			except:
				self.set_status(404)
				self.write(message(404, 'Failed to upload the file for dataset \"%s\"' % id))

		# determine which type of upload to do
		elif format == "file-path":
			try:
				# initialize input directory
				input_dir = os.path.join(env.DATASETS_DIR, id)
				os.makedirs(input_dir, exist_ok=True)

				# save uploaded files to input directory
				filenames = []

				for f_list in files.values():
					for f_arg in f_list:
						filename, body = f_arg['filename'], f_arg['body']
						# rename the filename based on 'parameter' query 'file-path'
						filename = parameter + os.path.splitext(os.path.basename(filename))[1]
						with open(os.path.join(input_dir, filename), 'wb') as f:
							f.write(body)
						filenames.append(filename)

				# increase nfiles
				dataset['nfiles'] += len(filenames)

				# save dataset
				await db.dataset_update(id, dataset)

				self.set_status(200)
				self.write(message(200, 'File \"%s\" was uploaded for dataset \"%s\" successfully' % (filenames, id)))
			except:
				self.set_status(404)
				self.write(message(404, 'Failed to upload the file for dataset \"%s\"' % id))

		# determine which type of upload to do
		else:
			self.set_status(404)
			self.write(message(404, 'The \"%s\" value for the query is not correct. Try with ["directory-path","file-path"]' % format))





#-------------------------------------
# WORKFLOW Classes
#-------------------------------------

class WorkflowQueryHandler(tornado.web.RequestHandler):

	async def get(self):
		page = int(self.get_query_argument('page', 0))
		page_size = int(self.get_query_argument('page_size', 100))

		db = self.settings['db']
		workflows = await db.workflow_query(page, page_size)

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode(workflows))



class WorkflowCreateHandler(tornado.web.RequestHandler):

	REQUIRED_KEYS = set([
		'pipeline',
		'revision',
		'profiles'
	])

	DEFAULTS = {
		'name': '',
		'author': '',
		'description': '',
		'revision': 'main',
		'profiles': 'guess',
		'attempts': 0,
		'params': []
	}

	def get(self):
		workflow = {**self.DEFAULTS, **{ '_id': '0' }}

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode(workflow))

	async def post(self):
		db = self.settings['db']

		# make sure request body is valid
		try:
			data = tornado.escape.json_decode(self.request.body)
			missing_keys = self.REQUIRED_KEYS - data.keys()
		except json.JSONDecodeError:
			self.set_status(422)
			self.write(message(422, 'Ill-formatted JSON'))
			return

		if missing_keys:
			self.set_status(400)
			self.write(message(400, 'Missing required field(s): %s' % list(missing_keys)))
			return

		# create workflow
		workflow = {**self.DEFAULTS, **data, **{ 'status': 'nascent' }}
		workflow['_id'] = str(bson.ObjectId())

		# append creation timestamp to workflow
		workflow['date_created'] = int(time.time() * 1000)

		# transform pipeline name to lowercase
		workflow['pipeline'] = workflow['pipeline'].lower() 

		# save workflow
		await db.workflow_create(workflow)

		# create workflow directory
		workflow_dir = os.path.join(env.WORKFLOWS_DIR, workflow['_id'])
		os.makedirs(workflow_dir)

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode({ '_id': workflow['_id'] }))




class WorkflowEditHandler(tornado.web.RequestHandler):

	REQUIRED_KEYS = set([])

	DEFAULTS = {
		'name': '',
		'author': '',
		'description': '',
		'revision': 'main',
		'profiles': 'guess',
		'attempts': 0,
		'params': []
	}

	async def get(self, id):
		db = self.settings['db']

		try:
			# get workflow
			workflow = await db.workflow_get(id)

			# append list of input files
			workflow_dir = os.path.join(env.WORKFLOWS_DIR, id)
			input_dir = os.path.join(workflow_dir, workflow['input_dir'])
			output_dir = os.path.join(workflow_dir, workflow['output_dir'])

			if os.path.exists(input_dir):
				workflow['files'] = list_dir_recursive(input_dir, relpath_start=workflow_dir)
			else:
				workflow['files'] = []

			# append list of output files
			if os.path.exists(output_dir):
				workflow['output_files'] = list_dir_recursive(output_dir, relpath_start=workflow_dir)
			else:
				workflow['output_files'] = []

			# append status of output data
			workflow['output_data'] = os.path.exists('%s/%s-output.tar.gz' % (workflow_dir, id))

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(workflow))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to get workflow \"%s\"' % id))

	async def post(self, id):
		db = self.settings['db']

		# make sure request body is valid
		try:
			data = tornado.escape.json_decode(self.request.body)
			missing_keys = self.REQUIRED_KEYS - data.keys()
		except json.JSONDecodeError:
			self.set_status(422)
			self.write(message(422, 'Ill-formatted JSON'))

		if missing_keys:
			self.set_status(400)
			self.write(message(400, 'Missing required field(s): %s' % list(missing_keys)))
			return

		try:
			# update workflow from request body
			workflow = await db.workflow_get(id)
			workflow = {**self.DEFAULTS, **workflow, **data}

			# transform pipeline name to lowercase
			workflow['pipeline'] = workflow['pipeline'].lower() 

			# save workflow
			await db.workflow_update(id, workflow)

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode({ '_id': id }))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to update workflow \"%s\"' % id))

	async def delete(self, id):
		db = self.settings['db']

		try:
			# delete workflow
			await db.workflow_delete(id)

			# delete workflow directory
			shutil.rmtree(os.path.join(env.WORKFLOWS_DIR, id), ignore_errors=True)

			self.set_status(200)
			self.write(message(200, 'Workflow \"%s\" was deleted' % id))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to delete workflow \"%s\"' % id))




class WorkflowLaunchHandler(tornado.web.RequestHandler):

	resume = True

	async def post(self, id):
		db = self.settings['db']

		try:
			# get workflow
			workflow = await db.workflow_get(id)

			# # make sure workflow is not already running
			# if workflow['status'] == 'running':
			# 	self.set_status(400)
			# 	self.write(message(400, 'Workflow \"%s\" is already running' % id))
			# 	return

			# copy nextflow.config from nextflow configration folder
			workflow_dir = os.path.join(env.WORKFLOWS_DIR, id)
			src = os.path.join(env.NXF_CONF, 'nextflow.config')
			dst = os.path.join(workflow_dir, 'nextflow.config')

			if os.path.exists(dst):
				os.remove(dst)   

			if os.path.exists(src):
				shutil.copyfile(src, dst)

			# append additional settings to nextflow.config
			with open(dst, 'a') as f:
				weblog_url = 'http://%s:%d/api/tasks' % (socket.gethostbyname(socket.gethostname()), tornado.options.options.port)
				f.write('weblog { enabled = true\n url = \"%s\" }\n' % (weblog_url))
				f.write('k8s { launchDir = \"%s\" }\n' % (workflow_dir))

			# update workflow status
			workflow['status'] = 'running'
			workflow['date_submitted'] = int(time.time() * 1000)
			workflow['attempts'] += 1

			# set up the output directory
			output_dir = os.path.join(env.OUTPUTS_DIR, id, str(workflow['attempts']))
			workflow['output_dir'] = output_dir

			await db.workflow_update(id, workflow)

			# launch workflow as a child process
			p = mp.Process(target=Workflow.launch, args=(db, workflow, self.resume))
			p.start()

			self.set_status(200)
			self.write(message(200, 'Workflow \"%s\" was launched' % id))
		# except:
		except Exception as e:
			self.set_status(404)
			# self.write(message(404, 'Failed to launch workflow \"%s\"' % id))
			self.write(message(404, 'Failed \"%s\"' % e))



class WorkflowResumeHandler(WorkflowLaunchHandler):

	resume = True



class WorkflowCancelHandler(tornado.web.RequestHandler):

	async def post(self, id):
		db = self.settings['db']

		try:
			# get workflow
			workflow = await db.workflow_get(id)
			workflow = {**{ 'pid': -1 }, **workflow}

			# cancel workflow
			Workflow.cancel(workflow)

			# update workflow status
			workflow['status'] = 'failed'
			workflow['pid'] = -1

			await db.workflow_update(id, workflow)

			self.set_status(200)
			self.write(message(200, 'Workflow \"%s\" was canceled' % id))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to cancel workflow \"%s\"' % id))



class WorkflowLogHandler(tornado.web.RequestHandler):

	async def get(self, id):
		db = self.settings['db']

		try:
			# get workflow
			workflow = await db.workflow_get(id)

			# append log if it exists
			log_file = os.path.join(env.WORKFLOWS_DIR, id, '.workflow.log')

			if os.path.exists(log_file):
				f = open(log_file)
				log = ''.join(f.readlines())
			else:
				log = ''

			# construct response data
			data = {
				'_id': id,
				'status': workflow['status'],
				'attempts': workflow['attempts'],
				'log': log
			}

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.set_header('cache-control', 'no-store, no-cache, must-revalidate, max-age=0')
			self.write(tornado.escape.json_encode(data))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to fetch log for workflow \"%s\"' % id))



class WorkflowDownloadHandler(tornado.web.StaticFileHandler):

	def parse_url_path(self, id):
		# provide output file if path is specified, otherwise output data archive
		filename_default = '%s-output.tar.gz' % id
		filename = self.get_query_argument('path', filename_default)

		self.set_header('content-disposition', 'attachment; filename=\"%s\"' % filename)
		return os.path.join(id, filename)



#-------------------------------------
# OUTPUTS Classes
#-------------------------------------

class OutputQueryHandler(tornado.web.RequestHandler):

	async def get(self):
		page = int(self.get_query_argument('page', 0))
		page_size = int(self.get_query_argument('page_size', 100))

		db = self.settings['db']
		datasets = await db.dataset_query(page, page_size)

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode(datasets))



class OutputLogHandler(tornado.web.RequestHandler):

	async def get(self, id):
		db = self.settings['db']

		try:
			# get workflow
			workflow = await db.workflow_get(id)

			# append log if it exists
			log_file = os.path.join(env.WORKFLOWS_DIR, id, '.workflow.log')

			if os.path.exists(log_file):
				f = open(log_file)
				log = ''.join(f.readlines())
			else:
				log = ''

			# construct response data
			data = {
				'_id': id,
				'status': workflow['status'],
				'attempts': workflow['attempts'],
				'log': log
			}

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.set_header('cache-control', 'no-store, no-cache, must-revalidate, max-age=0')
			self.write(tornado.escape.json_encode(data))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to fetch log for workflow \"%s\"' % id))



class OutputDownloadHandler(tornado.web.StaticFileHandler):

	def parse_url_path(self, id):
		# provide output file if path is specified, otherwise output data archive
		filename_default = '%s-output.tar.gz' % id
		filename = self.get_query_argument('path', filename_default)

		self.set_header('content-disposition', 'attachment; filename=\"%s\"' % filename)
		return os.path.join(id, filename)



#-------------------------------------
# TASKS Classes
#-------------------------------------

class TaskQueryHandler(tornado.web.RequestHandler):

	async def get(self):
		page = int(self.get_query_argument('page', 0))
		page_size = int(self.get_query_argument('page_size', 100))

		db = self.settings['db']
		tasks = await db.task_query(page, page_size)

		self.set_status(200)
		self.set_header('content-type', 'application/json')
		self.write(tornado.escape.json_encode(tasks))

	async def post(self):
		db = self.settings['db']

		# make sure request body is valid
		try:
			task = tornado.escape.json_decode(self.request.body)
		except json.JSONDecodeError:
			self.set_status(422)
			self.write(message(422, 'Ill-formatted JSON'))
			return

		try:
			# append id to task
			task['_id'] = str(bson.ObjectId())

			# extract input features for task
			if task['event'] == 'process_completed':
				# load execution log
				filenames = ['.command.log', '.command.out', '.command.err']
				filenames = [os.path.join(task['trace']['workdir'], filename) for filename in filenames]
				files = [open(filename) for filename in filenames if os.path.exists(filename)]
				lines = [line.strip() for f in files for line in f]

				# parse input features from trace directives
				PREFIX = '#TRACE'
				lines = [line[len(PREFIX):] for line in lines if line.startswith(PREFIX)]
				items = [line.split('=') for line in lines]
				conditions = {k.strip(): v.strip() for k, v in items}

				# append input features to task trace
				task['trace'] = {**task['trace'], **conditions}

			# save task
			await db.task_create(task)

			# update workflow status on completed event
			if task['event'] == 'completed':
				# get workflow
				workflow_id = task['runName'].split('-')[1]
				workflow = await db.workflow_get(workflow_id)

				# update workflow status
				success = task['metadata']['workflow']['success']
				if success:
					workflow['status'] = 'completed'
				else:
					workflow['status'] = 'failed'

				await db.workflow_update(workflow['_id'], workflow)

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode({ '_id': task['_id'] }))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to save task'))



class TaskLogHandler(tornado.web.RequestHandler):

	async def get(self, id):
		db = self.settings['db']

		try:
			# get workflow
			task = await db.task_get(id)
			workdir = task['trace']['workdir']

			# construct response data
			data = {
				'_id': id,
				'out': '',
				'err': ''
			}

			# append log files if they exist
			out_file = os.path.join(workdir, '.command.out')
			err_file = os.path.join(workdir, '.command.err')

			if os.path.exists(out_file):
				f = open(out_file)
				data['out'] = ''.join(f.readlines())

			if os.path.exists(err_file):
				f = open(err_file)
				data['err'] = ''.join(f.readlines())

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(data))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to fetch log for workflow \"%s\"' % id))



class TaskQueryPipelinesHandler(tornado.web.RequestHandler):

	async def get(self):
		db = self.settings['db']

		try:
			# query pipelines from database
			pipelines = await db.task_query_pipelines()

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(pipelines))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to perform query'))
			raise e



class TaskQueryPipelineHandler(tornado.web.RequestHandler):

	async def get(self, pipeline):
		db = self.settings['db']

		try:
			# query tasks from database
			pipeline = pipeline.lower()
			tasks = await db.task_query_pipeline(pipeline)
			tasks = [task['trace'] for task in tasks]

			# separate tasks into dataframes by process
			process_names = list(set([task['process'] for task in tasks]))
			dfs = {}

			for process in process_names:
				dfs[process] = [task for task in tasks if task['process'] == process]

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(dfs))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to perform query'))
			raise e



class TaskArchiveHandler(tornado.web.RequestHandler):

	async def get(self, pipeline):
		db = self.settings['db']

		try:
			# query tasks from database
			pipeline = pipeline.lower()
			tasks = await db.task_query_pipeline(pipeline)
			tasks = [task['trace'] for task in tasks]

			# separate tasks into dataframes by process
			process_names = list(set([task['process'] for task in tasks]))
			dfs = {}

			for process in process_names:
				dfs[process] = pd.DataFrame([task for task in tasks if task['process'] == process])

			# change to trace directory
			os.chdir(env.TRACES_DIR)

			# save dataframes to csv files
			for process in process_names:
				filename = 'trace.%s.txt' % (process)
				dfs[process].to_csv(filename, sep='\t', index=False)

			# create zip archive of trace files
			zipfile = 'trace.%s.zip' % (pipeline.replace('/', '__'))
			files = ['trace.%s.txt' % (process) for process in process_names]

			subprocess.run(['zip', zipfile] + files, check=True)
			subprocess.run(['rm', '-f'] + files, check=True)

			# return to working directory
			os.chdir('..')

			self.set_status(200)
			self.write(message(200, 'Archive was created'))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to create archive'))
			raise e



class TaskArchiveDownloadHandler(tornado.web.StaticFileHandler):

	def parse_url_path(self, pipeline):
		# get filename of trace archive
		filename = 'trace.%s.zip' % (pipeline.replace('/', '__'))

		self.set_header('content-disposition', 'attachment; filename=\"%s\"' % filename)
		return filename



class TaskVisualizeHandler(tornado.web.RequestHandler):

	async def post(self):
		db = self.settings['db']

		try:
			# parse request body
			data = tornado.escape.json_decode(self.request.body)

			# query task dataset from database
			pipeline = data['pipeline'].lower()
			tasks = await db.task_query_pipeline(pipeline)
			tasks = [task['trace'] for task in tasks]
			tasks_process = [task for task in tasks if task['process'] == data['process']]

			df = pd.DataFrame(tasks_process)

			# prepare visualizer args
			args = data['args']
			args['plot_name'] = str(bson.ObjectId())

			if args['selectors'] == '':
				args['selectors'] = []
			else:
				args['selectors'] = args['selectors'].split(' ')

			# append columns from merge process if specified
			if 'merge_process' in args:
				# load merge data
				tasks_merge = [task for task in tasks if task['process'] == args['merge_process']]
				df_merge = pd.DataFrame(tasks_merge)

				# remove duplicate columns
				dupe_columns = set(df.columns).intersection(df_merge.columns)
				dupe_columns.remove(args['merge_key'])
				df_merge.drop(columns=dupe_columns, inplace=True)

				# append merge columns to data
				df = df.merge(df_merge, on=args['merge_key'], how='left', copy=False)

			# create visualization
			outfile = Visualizer.visualize(df, args)

			# encode image file into base64
			with open(outfile, 'rb') as f:
				image_data = base64.b64encode(f.read()).decode('utf-8')

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(image_data))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to visualize data'))
			raise e



class TaskEditHandler(tornado.web.RequestHandler):

	async def get(self, id):
		db = self.settings['db']

		try:
			task = await db.task_get(id)

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(task))
		except:
			self.set_status(404)
			self.write(message(404, 'Failed to get task \"%s\"' % id))



class ModelTrainHandler(tornado.web.RequestHandler):

	async def post(self):
		db = self.settings['db']

		try:
			# parse request body
			data = tornado.escape.json_decode(self.request.body)

			# query task dataset from database
			pipeline = data['pipeline'].lower()
			tasks = await db.task_query_pipeline(pipeline)
			tasks = [task['trace'] for task in tasks]
			tasks_process = [task for task in tasks if task['process'] == data['process']]

			df = pd.DataFrame(tasks_process)

			# prepare training args
			args = data['args']
			args['hidden_layer_sizes'] = [int(v) for v in args['hidden_layer_sizes'].split(' ')]
			args['model_name'] = '%s.%s.%s' % (pipeline.replace('/', '__'), data['process'], args['target'])

			if args['selectors'] == '':
				args['selectors'] = []
			else:
				args['selectors'] = args['selectors'].split(' ')

			# append columns from merge process if specified
			if args['merge_process'] != None:
				# load merge data
				tasks_merge = [task for task in tasks if task['process'] == args['merge_process']]
				df_merge = pd.DataFrame(tasks_merge)

				# remove duplicate columns
				dupe_columns = set(df.columns).intersection(df_merge.columns)
				dupe_columns.remove(args['merge_key'])
				df_merge.drop(columns=dupe_columns, inplace=True)

				# append merge columns to data
				df = df.merge(df_merge, on=args['merge_key'], how='left', copy=False)

			# train model
			results = Model.train(df, args)

			# visualize training results
			df = pd.DataFrame()
			df['y_true'] = results['y_true']
			df['y_pred'] = results['y_pred']

			outfile = Visualizer.visualize(df, {
				'xaxis': 'y_true',
				'yaxis': 'y_pred',
				'plot_name': str(bson.ObjectId())
			})

			# encode image file into base64
			with open(outfile, 'rb') as f:
				results['scatterplot'] = base64.b64encode(f.read()).decode('utf-8')

			# remove extra fields from results
			del results['y_true']
			del results['y_pred']

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(results))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to train model'))
			raise e



class ModelConfigHandler(tornado.web.RequestHandler):

	async def get(self):
		try:
			# parse request body
			pipeline = self.get_argument('pipeline', default=None)
			process = self.get_argument('process', default=None)
			target = self.get_argument('target', default=None)

			# get model config file
			filename = '%s/%s.%s.%s.json' % (env.MODELS_DIR, pipeline.lower().replace('/', '__'), process, target)

			with open(filename, 'r') as f:
				config = json.load(f)

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(config))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to get model config'))
			raise e



class ModelPredictHandler(tornado.web.RequestHandler):

	async def post(self):
		try:
			# parse request body
			data = tornado.escape.json_decode(self.request.body)
			data['pipeline'] = data['pipeline'].lower()
			data['model_name'] = '%s.%s.%s' % (data['pipeline'].replace('/', '__'), data['process'], data['target'])

			# perform model prediction
			results = Model.predict(data['model_name'], data['inputs'])

			self.set_status(200)
			self.set_header('content-type', 'application/json')
			self.write(tornado.escape.json_encode(results))
		except Exception as e:
			self.set_status(404)
			self.write(message(404, 'Failed to perform model prediction'))
			raise e



if __name__ == '__main__':
	# parse command-line options
	tornado.options.define('backend', default='mongo', help='Database backend to use (file or mongo)')
	tornado.options.define('url-file', default='db.pkl', help='database file for file backend')
	tornado.options.define('url-mongo', default='localhost', help='mongodb service url for mongo backend')
	tornado.options.define('np', default=1, help='number of server processes')
	tornado.options.define('port', default=8080)
	tornado.options.parse_command_line()

	# initialize auxiliary directories
	os.makedirs(env.WORKFLOWS_DIR, exist_ok=True)
	os.makedirs(env.DATASETS_DIR, exist_ok=True)
	os.makedirs(env.OUTPUTS_DIR, exist_ok=True)
	os.makedirs(env.TRACES_DIR, exist_ok=True)
	os.makedirs(env.MODELS_DIR, exist_ok=True)

	# initialize api endpoints
	app = tornado.web.Application([
		(r'/api/datasets', DatasetQueryHandler),
		(r'/api/datasets/0', DatasetCreateHandler),
		(r'/api/datasets/([a-zA-Z0-9-]+)', DatasetEditHandler),
		(r'/api/datasets/([a-zA-Z0-9-]+)/([a-zA-Z-]+)/([a-zA-Z0-9-_]+)/upload', DatasetUploadHandler),

		(r'/api/workflows', WorkflowQueryHandler),
		(r'/api/workflows/0', WorkflowCreateHandler),
		(r'/api/workflows/([a-zA-Z0-9-]+)', WorkflowEditHandler),
		(r'/api/workflows/([a-zA-Z0-9-]+)/launch', WorkflowLaunchHandler),
		(r'/api/workflows/([a-zA-Z0-9-]+)/resume', WorkflowResumeHandler),
		(r'/api/workflows/([a-zA-Z0-9-]+)/cancel', WorkflowCancelHandler),
		(r'/api/workflows/([a-zA-Z0-9-]+)/log', WorkflowLogHandler),
		(r'/api/workflows/([a-zA-Z0-9-]+)/download', WorkflowDownloadHandler, dict(path=env.WORKFLOWS_DIR)),

		(r'/api/outputs', OutputQueryHandler),
		(r'/api/outputs/([a-zA-Z0-9-]+)/log', OutputLogHandler),
		(r'/api/outputs/([a-zA-Z0-9-]+)/download', OutputDownloadHandler, dict(path=env.OUTPUTS_DIR)),

		(r'/api/tasks', TaskQueryHandler),
		(r'/api/tasks/([a-zA-Z0-9-]+)/log', TaskLogHandler),
		(r'/api/tasks/pipelines', TaskQueryPipelinesHandler),
		(r'/api/tasks/pipelines/(.+)', TaskQueryPipelineHandler),
		(r'/api/tasks/archive/(.+)/download', TaskArchiveDownloadHandler, dict(path=env.TRACES_DIR)),
		(r'/api/tasks/archive/(.+)', TaskArchiveHandler),
		(r'/api/tasks/visualize', TaskVisualizeHandler),
		(r'/api/tasks/([a-zA-Z0-9-]+)', TaskEditHandler),

		(r'/api/model/train', ModelTrainHandler),
		(r'/api/model/config', ModelConfigHandler),
		(r'/api/model/predict', ModelPredictHandler),
		(r'/(.*)', tornado.web.StaticFileHandler, dict(path='./client', default_filename='index.html'))
	])

	try:
		# spawn server processes
		server = tornado.httpserver.HTTPServer(app, max_buffer_size=1024 ** 100)
		server.bind(tornado.options.options.port)
		server.start(tornado.options.options.np)

		# connect to database
		if tornado.options.options.backend == 'file':
			app.settings['db'] = backend.FileBackend(tornado.options.options.url_file)

		elif tornado.options.options.backend == 'mongo':
			app.settings['db'] = backend.MongoBackend(tornado.options.options.url_mongo)

		else:
			raise KeyError('Backend must be either \'json\' or \'mongo\'')

		# start the event loop
		print('The API is listening on http://0.0.0.0:%d' % (tornado.options.options.port), flush=True)
		tornado.ioloop.IOLoop.current().start()

	except KeyboardInterrupt:
		tornado.ioloop.IOLoop.current().stop()
