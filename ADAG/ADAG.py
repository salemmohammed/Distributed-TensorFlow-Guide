"""Asynchronous Distributed Adaptive Gradients (ADAG)
Performs asynchronous updates with update window. 

Author: Tommy Mulc
"""

from __future__ import print_function
import tensorflow as tf
import argparse
import time
import os
FLAGS = None
log_dir = '/logdir'

def main():
	# Configure
	config=tf.ConfigProto(log_device_placement=False)

	#Server Setup
	cluster_spec = {
  			'ps':['localhost:2222'],
  			'worker':['localhost:2223','localhost:2224']
  			} #allows this node know about all other nodes
	n_pss = len(cluster_spec['ps']) #the number of parameter servers
	n_workers = len(cluster_spec['worker']) #the number of worker nodes
	cluster = tf.train.ClusterSpec(cluster_spec)

	if FLAGS.job_name == 'ps': #checks if parameter server
		server = tf.train.Server(cluster,
					job_name="ps",
					task_index=FLAGS.task_index,
					config=config)
		server.join()
	else: #it must be a worker server
		is_chief = (FLAGS.task_index == 0) #checks if this is the chief node
		server = tf.train.Server(cluster,
					job_name="worker",
					task_index=FLAGS.task_index,
					config=config)
		
		# Graph
		# We must not use train.replicate_device_setter for normal operations
		# Local operations
		with tf.device("/job:worker/replica:0/task:%d" % FLAGS.task_index):
			a = tf.Variable(tf.constant(0.,shape=[2]),dtype=tf.float32,
						collections=[tf.GraphKeys.LOCAL_VARIABLES])
			b = tf.Variable(tf.constant(0.,shape=[2]),dtype=tf.float32,
						collections=[tf.GraphKeys.LOCAL_VARIABLES])
			c=a+b
			local_step = tf.Variable(0,dtype=tf.int32,trainable=False,
						name='local_step',collections=['local_non_trainable'])

		with tf.device(tf.train.replica_device_setter(ps_tasks=n_pss,
        	worker_device="/job:%s/task:%d" % (FLAGS.job_name,FLAGS.task_index))):
			global_step = tf.Variable(0,dtype=tf.int32,trainable=False,name='global_step')
			target = tf.constant(100.,shape=[2],dtype=tf.float32)
			loss = tf.reduce_mean(tf.square(c-target))

			# all workers use the same learning rate and it is decided on by the task 0 
			# or maybe the from the graph of the chief worker
			lr = .0001
			loptimizer = tf.train.GradientDescentOptimizer(lr) #local optimizer
			optimizer = tf.train.GradientDescentOptimizer(lr) #the learning rate set here is global

			#create global variables and/or references
			local_to_global, global_to_local = create_global_variables()
		
			# ADAG (simplest case since all batches are the same)
			update_window = 3 # T: update/communication window
			grad_list = [] # the array to store the gradients through the communication window
			for t in range(update_window):
				if t != 0:
					with tf.control_dependencies([opt_local]): #compute gradients only if the local opt was run
						grads, varss = zip(*loptimizer.compute_gradients(loss,
									var_list=tf.local_variables()))
				else:
					grads, varss = zip(*loptimizer.compute_gradients(loss,
								var_list=tf.local_variables())) 
				grad_list.append(grads) #add gradients to the list
				opt_local = loptimizer.apply_gradients(zip(grads,varss),
							global_step=local_step) #update local parameters
			grads = tf.reduce_mean(grad_list,axis=0)
			grads = tuple([grads[i]for i in range(len(varss))])
			opt = optimizer.apply_gradients(
						zip(grads,[ local_to_global[v] for v in varss])
						,global_step=global_step) #apply the gradients to variables on ps

			# Pull param from global server
			with tf.control_dependencies([opt]):
				assign_locals = assign_global_to_local(global_to_local)

			# Init ops
			init_local = tf.variables_initializer(tf.local_variables() \
					+tf.get_collection('local_non_trainable'))#for local variables
			init = tf.global_variables_initializer() # for global variables

			# Grab global state before training so all workers have same initialization
			grab_global_init = assign_global_to_local(global_to_local)

			# Assigns local values to global ones for chief to execute
			assign_global = assign_local_to_global(local_to_global)

		# Session
		stop_hook = tf.train.StopAtStepHook(last_step=40)
		hooks = [stop_hook]
		scaff = tf.train.Scaffold(init_op=init,local_init_op=init_local)

		#Monitored Training Session
		sess = tf.train.MonitoredTrainingSession(master=server.target,
					is_chief=is_chief,
					config=config,
					scaffold=scaff,
					hooks=hooks,
					save_checkpoint_secs=1,
					checkpoint_dir='logdir')
		if is_chief:
			sess.run(assign_global) #Assigns chief's initial values to ps
			time.sleep(10) #grace period to wait on other workers before starting training

		# Train until hook stops session
		print('Starting training on worker %d'%FLAGS.task_index)
		sess.run(grab_global_init)
		while not sess.should_stop():
			_,_,r,gs,ls = sess.run([opt,assign_locals,c,global_step,local_step])
			print(r,"global step: "+str(gs),"worker: "+str(FLAGS.task_index),"local step: "+str(ls))
			if gs % 7 == 1:
				for j in grad_list:
					print(sess.run(j),FLAGS.task_index)

			time.sleep(1)
		print('Done',FLAGS.task_index)

		time.sleep(10) #grace period to wait before closing session
		sess.close()
		print('Session from worker %d closed cleanly'%FLAGS.task_index)


def assign_global_to_local(global_to_local):
	"""
	global_to_local : dictionary with corresponding local variable for global key

	Assigns global variable value to local variables
	"""
	r = []
	for v in global_to_local.keys():
		r.append(tf.assign(global_to_local[v],v))
	with tf.control_dependencies(r):
		a = tf.no_op()
	return a


def assign_local_to_global(local_to_global):
	"""Assigns global variable value to local variables.

	local_to_global : dictionary with corresponding global variable for local key
	"""
	r= []
	for v in local_to_global.keys():
		r.append(tf.assign(local_to_global[v],v))
	with tf.control_dependencies(r):
		a = tf.no_op()
	return a


def get_global_variable_by_name(name):
	"""Returns the global variable of given name.

	name : the name of the global variable
	"""
	return [v for v in tf.global_variables() if v.name == name][0]


def create_global_variables():
	"""Creates global variables for local variables on the graph.

	Returns dictionarys for local-to-global and global-to-local
	variable mappings.
	"""
	local_to_global = {}
	global_to_local = {}
	with tf.device('/job:ps/task:0'):
		for v in tf.local_variables():
			v_g = tf.get_variable('g/'+v.op.name,
				shape = v.shape,
				dtype = v.dtype,
				trainable=True,
				collections=[tf.GraphKeys.GLOBAL_VARIABLES,tf.GraphKeys.TRAINABLE_VARIABLES])
			local_to_global[v] = v_g
			global_to_local[v_g] = v
	return local_to_global,global_to_local


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	# Flags for defining the tf.train.ClusterSpec
	parser.add_argument(
    	"--job_name",
    	type=str,
    	default="",
    	help="One of 'ps', 'worker'"
    )
  # Flags for defining the tf.train.Server
	parser.add_argument(
    	"--task_index",
    	type=int,
    	default=0,
    	help="Index of task within the job"
    )
	FLAGS, unparsed = parser.parse_known_args()
	print(FLAGS.task_index)
	main()
