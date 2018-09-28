# -*- coding: utf-8 -*-
import os
import datetime
import shutil
import subprocess
from subprocess import Popen, PIPE
import json
import logging
import logging.handlers

from flask import Flask, request, render_template, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from celery import Celery
from celery.signals import task_success
import redis

from .ansible_api import MyRunner

app = Flask(__name__)
mail = Mail(app)
redis = redis.Redis(host='172.19.3.165', port=6379)

db = SQLAlchemy(app)


class DeployLog(db.Model):
    __tablename__ = 'webhook_deploy_log'

    id = db.Column(db.Integer, primary_key=True)
    project = db.Column(db.String(32))
    version = db.Column(db.String(64))
    deployer = db.Column(db.String(16))
    environment = db.Column(db.String(16))
    summary = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=db.func.now())
    updated_at = db.Column(
        db.DateTime, default=db.func.now(), onupdate=db.func.now())

    def __repr__(self):
        return '<User %r>' % self.project


app.config[
    'SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://boer:123456@172.19.3.165/deploy_ops_dev'

app.config['CELERY_BROKER_URL'] = 'amqp://boer:123456@172.19.3.165/webhook'
app.config['CELERY_RESULT_BACKEND'] = 'redis://172.19.3.165:6379'

app.config['MAIL_SERVER'] = '<yourmailserver>'
app.config['MAIL_PORT'] = 25
app.config['MAIL_USERNAME'] = 'boer'
app.config['MAIL_PASSWORD'] = '<yourmailpassword>'

app.config['CHECKOUT_DIR'] = '/data/deployment/gitrepos'
app.config['DEPLOY_DIR'] = '/data/deployment/deploydir'
app.config['PROJECTS_DIR'] = '/data/deployment/projects'

app.config['RESOURCE'] = {
    'weekfix-web': {
        'hosts': [{
            'hostname': '172.19.3.24',
            'port': 22
        }, {
            'hostname': '172.19.3.25',
            'port': 22
        }],
        'vars': {
            'ansible_user': 'app',
            'ansible_become': True,
            'ansible_become_method': 'sudo',
            'ansible_become_user': 'root',
            'ansible_become_pass': '<password>'
        }
    },
    'feature-web': {
        'hosts': [{
            'hostname': '172.19.3.28',
            'port': 22
        }, {
            'hostname': '172.19.3.29',
            'port': 22
        }],
        'vars': {
            'ansible_user': 'app',
            'ansible_become': True,
            'ansible_become_method': 'sudo',
            'ansible_become_user': 'root',
            'ansible_become_pass': '<password>'
        }
    }
}

from tasks import make_celery
celery = make_celery(app)


@app.before_first_request
def setup_logging():
    if not app.debug:
        handler = logging.handlers.TimedRotatingFileHandler(
            '/tmp/deploy_gitlab_webhook_logging.log',
            when='D',
            backupCount=7,
            encoding='utf-8')
        # handler = logging.FileHandler('/tmp/deploy_gitlab_webhook_logging.log')
        logging_format = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(filename)s - %(funcName)s - %(lineno)s - %(message)s'
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging_format)
        app.logger.addHandler(handler)


def update_repo(repo_path, repo_url, commit_id):
    git_path = os.path.join(repo_path, '.git')
    if os.path.exists(git_path) and os.path.isdir(git_path):
        # command 有&&就要shell=True
        cmd = 'cd %s && git reset -q --hard origin/master && git fetch --prune -q' % repo_path
        rc = subprocess.check_call(cmd, shell=True, cwd=repo_path)
        os.chdir(repo_path)
    else:
        if os.path.exists(os.path.dirname(repo_path)) and os.path.isdir(
                os.path.dirname(repo_path)):
            shutil.rmtree(os.path.dirname(repo_path))
        else:
            os.makedirs(os.path.dirname(repo_path))
        cmd = 'git clone -q %s %s' % (repo_url, os.path.basename(repo_path))
        rc = subprocess.check_call(cmd.split(), cwd=os.path.dirname(repo_path))
        os.chdir(repo_path)
    # 指定commit_id
    cmd = 'git reset -q --hard %s' % commit_id
    rc = subprocess.check_call(cmd.split(), cwd=repo_path)


def rsync_local(src, dest, excludes=[]):
    excludes.append('.git')
    exclude_args = ''
    for e in excludes:
        exclude_args = exclude_args + ' --exclude %s' % e
    cmd = 'rsync -qa --delete %s %s%s %s%s' % (exclude_args, src, os.sep, dest,
                                               os.sep)
    rc = subprocess.check_call(cmd.split())


def chk_and_set_exe(src_path):
    if not os.access(src_path, os.X_OK):
        os.chmod(src_path, 755)


@celery.task
def exec_custom_cmd(script_file):
    if os.path.exists(script_file) and os.path.isfile(script_file):
        chk_and_set_exe(script_file)
        # output = subprocess.check_output(script_file, shell=False)
        # return output
        p = Popen(script_file, stdout=PIPE)
        c_pid = p.pid
        output = p.communicate()[0]
        rc = p.wait()
        if rc == 0:
            result = output
            deploy_type = json.loads(redis.rpop('deploy_type'))
            recipients = json.loads(redis.rpop('recipients'))
            carbon_copy = json.loads(redis.rpop('carbon_copy'))
            functions = json.loads(redis.rpop('functions'))
            restart_service = json.loads(redis.rpop('restart_service'))
            subject = json.loads(redis.rpop('subject'))
            name = json.loads(redis.rpop('name'))
            repo_name = json.loads(redis.rpop('repo_name'))
            commit_id = json.loads(redis.rpop('commit_id'))
            commit_msg = json.loads(redis.rpop('commit_msg'))
            _deploy_log = DeployLog(
                project=repo_name,
                version=commit_id,
                deployer=name,
                environment='30环境' if deploy_type == 'weekfix' else '31环境',
                summary=commit_msg)
            db.session.add(_deploy_log)
            db.session.commit()
            if restart_service:
                restarted_phpfpm_service.delay()
            send_async_email.delay(
                'boer@heclouds.com',
                recipients,
                carbon_copy,
                subject,
                'deploy',
                name=name,
                repo_name=repo_name,
                commit_id=commit_id,
                introduce=commit_msg,
                functions=functions,
                deploy_type=deploy_type,
                result=result)
        else:
            raise subprocess.CalledProcessError()


# 发邮件任务
@celery.task
def send_async_email(sender, to, cc, subject, template, **kwargs):
    msg = Message(subject, sender=sender, recipients=to, cc=cc)
    # msg.body = render_template(template + '.txt', **kwargs)
    msg.html = render_template(template + '.html', **kwargs)
    mail.send(msg)


# 发邮件任务
@celery.task
def send_email(sender, to, cc, subject, body, **kwargs):
    msg = Message(subject, sender=sender, recipients=to, cc=cc)
    msg.body = body
    mail.send(msg)


# 重启php7-fpm服务
@celery.task
def restarted_phpfpm_service():
    rc = subprocess.check_call(
        'sudo -u app ansible web -o -m service -a "name=php7-fpm state=restarted"',
        shell=True)
    # print(output)


# exec_custom_cmd执行成功后,触发信号(重启服务/发送邮件)
# @task_success.connect(sender=exec_custom_cmd)
# def at_task_success_todo(result, sender=None, **kwargs):
#     deploy_type = json.loads(redis.rpop('deploy_type'))
#     recipients = json.loads(redis.rpop('recipients'))
#     carbon_copy = json.loads(redis.rpop('carbon_copy'))
#     functions = json.loads(redis.rpop('functions'))
#     restart_service = json.loads(redis.rpop('restart_service'))
#     subject = json.loads(redis.rpop('subject'))
#     name = json.loads(redis.rpop('name'))
#     repo_name = json.loads(redis.rpop('repo_name'))
#     commit_id = json.loads(redis.rpop('commit_id'))
#     commit_msg = json.loads(redis.rpop('commit_msg'))
#     if restart_service:
#         restarted_phpfpm_service.delay()
#     send_async_email.delay(
#         'boer@heclouds.com',
#         recipients,
#         carbon_copy,
#         subject,
#         'deploy',
#         name=name,
#         repo_name=repo_name,
#         commit_id=commit_id,
#         introduce=commit_msg,
#         functions=functions,
#         result=result)


@app.route('/', methods=['POST'])
def index():
    event = request.headers.get('X-Gitlab-Event')
    if event is None and event != 'Note Hook':
        return ''
    results = json.loads(request.data)
    noteable_type = results['object_attributes']['noteable_type']
    # Triggered when a new comment is made on commits, merge requests, issues, and code snippets.
    if noteable_type != 'Commit':
        return '', 404
    # user 相关
    name = results['user']['name']
    username = results['user']['username']
    # if username not in ['zhanghaibo', 'zhangqiaojuan']:
    #     send_email.delay('boer@heclouds.com', ['boer0924@qq.com'], None, u'无权限操作', username + u'无权限操作')
    #     return '', 403
    # repository 相关
    repo_name = results['repository']['name']
    if repo_name not in [
            'onenet_v3', 'onenet_ee', 'admin_onenetv3', 'passport',
            'questionnaire', 'campaignmap', 'forum_v2', 'hachi_lib',
            'phpcorelib', 'groupservice', 'onenet_h5', 'app_editor', 'iotbox',
            'admin_iotbox'
    ]:
        send_email.delay(
            'boer@heclouds.com', ['boer0924@qq.com'], None, u'未知项目',
            name + ' - ' + repo_name + ' - ' + commit_id + u' - 未知项目')
        return '', 400
    repo_url = results['repository']['url']
    repo_path = os.path.join(app.config['CHECKOUT_DIR'], repo_name, repo_name)
    # commit 相关
    commit_id = results['commit']['id']
    # app.logger.info(commit_id)
    commit_msg = results['commit']['message'].strip('\n')
    # object_attributes 相关
    try:
        notes = results['object_attributes']['note']
        if not notes.startswith('```json') and not notes.endswith('```'):
            return ''
        notes = notes.lstrip('```json').rstrip('```').replace('\r\n', '')
        notes = json.loads(notes)
    except Exception as e:
        send_email.delay(
            'boer@heclouds.com', ['boer0924@qq.com'], None, u'评论json格式错误',
            name + ' - ' + repo_name + ' - ' + commit_id + u' - 评论格式错误')
        return '', 400
    deploy_type = notes.get('deploy_type')
    test_name = notes.get('test_name')
    if test_name is None:
        test_name = str(notes.get('test_name'))
    recipients = notes.get('recipients')
    carbon_copy = notes.get('carbon_copy')
    functions = notes.get('functions')
    restart_service = notes.get('restart_service')
    update_repo(repo_path, repo_url, commit_id)
    rsync_local(repo_path,
                os.path.join(app.config['DEPLOY_DIR'], repo_name, repo_name))

    script_file = ''
    subject = ''
    host_lists = []
    ansible_playbook = ''
    if repo_name in [
            'onenet_v3', 'onenet_ee', 'admin_onenetv3', 'passport',
            'questionnaire', 'onenet_h5', 'iotbox', 'admin_iotbox'
    ]:
        if deploy_type == 'weekfix':
            script_file = os.path.join(app.config['PROJECTS_DIR'],
                                       repo_name + '_weekfix',
                                       'script/local_after.sh')
            subject = datetime.datetime.strftime(
                datetime.date.today(), '%Y/%m/%d'
            ) + ' [' + repo_name + ' - ' + test_name + '] weekfix_30testing'
        elif deploy_type == 'hotfix':
            script_file = os.path.join(app.config['PROJECTS_DIR'],
                                       repo_name + '_feature',
                                       'script/local_after.sh')
            subject = datetime.datetime.strftime(
                datetime.date.today(), '%Y/%m/%d'
            ) + ' [' + repo_name + ' - ' + test_name + '] hotfix_31testing'
        elif deploy_type == 'feature':
            script_file = os.path.join(app.config['PROJECTS_DIR'],
                                       repo_name + '_feature',
                                       'script/local_after.sh')
            subject = datetime.datetime.strftime(
                datetime.date.today(), '%Y/%m/%d'
            ) + ' [' + repo_name + ' - ' + test_name + '] feature_31testing'
        else:
            send_email.delay('boer@heclouds.com', ['boer0924@qq.com'], None,
                             u'未匹配的部署类型', name + '-' + repo_name + ' - ' +
                             commit_id + u' - 未匹配的部署类型 -' + deploy_type)
            return '', 400
    else:
        script_file = os.path.join(app.config['PROJECTS_DIR'], repo_name,
                                   'script/local_after.sh')
        subject = datetime.datetime.strftime(
            datetime.date.today(), '%Y/%m/%d'
        ) + ' [' + repo_name + ' - ' + test_name + '] all_30/31_testing'
    redis.lpush('name', json.dumps(name))
    redis.lpush('repo_name', json.dumps(repo_name))
    redis.lpush('commit_id', json.dumps(commit_id))
    redis.lpush('commit_msg', json.dumps(commit_msg))
    redis.lpush('deploy_type', json.dumps(deploy_type))
    redis.lpush('recipients', json.dumps(recipients))
    redis.lpush('carbon_copy', json.dumps(carbon_copy))
    redis.lpush('functions', json.dumps(functions))
    redis.lpush('restart_service', json.dumps(restart_service))
    redis.lpush('subject', json.dumps(subject))
    # 软链/权限/打包/编译
    exec_custom_cmd.delay(script_file)
    return ''


@app.route('/history')
def history():
    return render_template('history.html')


@app.route('/api/history')
def api_history():
    project = request.args.get('p', 'onenet_v3')
    environment = '30环境' if request.args.get('e', '30') == '30' else '31环境'
    _project = DeployLog.query.filter_by(
        project=project, environment=environment).order_by(
            DeployLog.id.desc()).first()
    if _project is None:
        return jsonify(code=100404)
    return jsonify(
        code=100200,
        project=_project.project,
        deployer=_project.deployer,
        environment=_project.environment,
        created_at=_project.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        summary=_project.summary)
