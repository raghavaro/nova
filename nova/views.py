import os
import io
import re
from functools import wraps
from nova import app, db, login_manager, fs, logic, memtar, tasks, models, es
from nova.models import (User, Collection, Dataset, SampleScan, Genus, Family,
                         Order, Access, Notification, Process, Bookmark)
from flask import (Response, render_template, request, flash, redirect,
                   url_for, jsonify, send_from_directory, abort)
from flask_login import login_user, logout_user, current_user
from flask_wtf import Form
from flask_sqlalchemy import Pagination
from wtforms import StringField, BooleanField
from wtforms.validators import DataRequired


def login_required(admin=False):
    def wrapper(func):
        @wraps(func)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()

            if admin and not current_user.is_admin:
                return login_manager.unauthorized()

            return func(*args, **kwargs)
        return decorated_view
    return wrapper


class LoginForm(Form):
    name = StringField('name', validators=[DataRequired()])
    password = StringField('password', validators=[DataRequired()])


class SignupForm(Form):
    name = StringField('name', validators=[DataRequired()])
    fullname = StringField('fullname', validators=[DataRequired()])
    email = StringField('email', validators=[DataRequired()])
    password = StringField('password', validators=[DataRequired()])
    is_admin = BooleanField('is_admin')


class CreateForm(Form):
    name = StringField('name', validators=[DataRequired()])
    description = StringField('description')


class CreateCollectionForm(Form):
    name = StringField('name', validators=[DataRequired()])
    description = StringField('description')


class RunCommandForm(Form):
    name = StringField('name', validators=[DataRequired()])
    command_line = StringField('command-line', validators=[DataRequired()])
    input = StringField('input', validators=[DataRequired()])
    output = StringField('output', validators=[DataRequired()])


class SearchForm(Form):
    query = StringField('query', validators=[DataRequired()])


class ImportForm(Form):
    path_template = StringField('template', validators=[DataRequired()])


class InvalidUsage(Exception):
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)

        self.message = message
        self.payload = payload
        self.status_code = status_code or self.status_code

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv


@app.errorhandler(InvalidUsage)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


@app.errorhandler(404)
def handle_page_not_found(error):
    print error.description
    return render_template('404.html', description=error.description), 404


@login_manager.user_loader
def load_user(user_id):
    return db.session.query(User).filter(User.name == user_id).first()


@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()

    if form.validate_on_submit():
        user = db.session.query(User).filter(User.name == form.name.data).first()

        if user.password == form.password.data:
            login_user(user)

            flash('Logged in successfully')
            response = app.make_response(redirect(url_for('index')))
            response.set_cookie('token', user.token)
            return response
        else:
            return render_template('user/login.html', form=form, failed=True), 401

    return render_template('user/login.html', form=form, failed=False)


@app.route('/logout')
@login_required(admin=False)
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/')
@app.route('/<int:page>')
@login_required(admin=False)
def index(page=1):
    if current_user.first_time:
        current_user.first_time = False
        db.session.commit()
        return render_template('index/welcome.html', user=current_user)

    shared = db.session.query(Dataset, Access).\
        filter(Access.user == current_user).\
        filter(Access.dataset_id == Dataset.id).\
        filter(Access.owner == False).\
        filter(Access.seen == False).\
        all()

    shared, shared_accesses = zip(*shared) if shared else ([], [])

    for access in shared_accesses:
        access.seen = True

    db.session.commit()

    return render_template('index/index.html')


@app.route('/settings')
@login_required(admin=True)
def admin():
    users = db.session.query(User).all()
    return render_template('user/admin.html', users=users)


@app.route('/token/generate')
@login_required(admin=False)
def generate_token():
    current_user.generate_token()
    return redirect('user/{}'.format(current_user.name))


@app.route('/token/revoke')
@login_required(admin=False)
def revoke_token():
    current_user.token = None
    db.session.commit()
    return redirect('user/{}'.format(current_user.name))


@app.route('/signup', methods=['GET', 'POST'])
@login_required(admin=True)
def signup():
    form = SignupForm()

    if form.validate_on_submit():
        user = User(name=form.name.data, fullname=form.fullname.data,
                    email=form.email.data, password=form.password.data,
                    is_admin=form.is_admin.data)
        db.session.add(user)
        db.session.commit()
        user.generate_token()
        return redirect(url_for('admin'))

    return render_template('user/signup.html', form=form)


@app.route('/user/<name>')
@app.route('/user/<name>/<int:page>')
@login_required(admin=False)
def profile(name, page=1):
    user = db.session.query(User).filter(User.name == name).first()
    pagination = Collection.query.\
        filter(Collection.user == user).\
        paginate(page=page, per_page=8)
    return render_template('user/profile.html', user=user, pagination=pagination)


@app.route('/user/<name>/bookmarks')
def list_bookmarks(name):
    user = db.session.query(User).filter(User.name == name).first()
    return render_template('user/bookmarks.html', user=user)



@app.route('/create/<collection_name>', methods=['GET', 'POST'])
@login_required(admin=False)
def create_dataset(collection_name):
    form = CreateForm()
    collection = Collection.query.\
        filter(Collection.name == collection_name).\
        filter(Collection.user == current_user).\
        first()

    if form.validate_on_submit():
        logic.create_dataset(models.SampleScan, form.name.data, current_user,
                collection, description=form.description.data)
        return redirect(url_for('index'))

    return render_template('dataset/create.html', form=form, collection=collection)


@app.route('/foo', methods=['GET', 'POST'])
@login_required(admin=False)
def create_collection():
    form = CreateCollectionForm()

    if form.validate_on_submit():
        logic.create_collection(form.name.data, current_user, form.description.data)
        return redirect(url_for('index'))

    return render_template('collection/create.html', form=form)


@app.route('/import', methods=['POST'])
@login_required(admin=False)
def import_submission():
    form = ImportForm()

    # FIXME: again this is not working
    # if form.validate_on_submit():
    #     pass
    template = request.form['template']

    # XXX: incredible danger zone!
    for entry in (e for e in os.listdir(template) if os.path.isdir(os.path.join(template, e))):
        path = os.path.join(template, entry)
        app.logger.info("Importing {}".format(entry))
        logic.import_sample_scan(entry, current_user, path)

    return redirect(url_for('index'))


@app.route('/update', methods=['POST'])
@login_required(admin=False)
def update():
    import csv

    # XXX: more danger zone!
    genuses = {x.name: x for x in db.session.query(Genus).all()}
    families = {x.name: x for x in db.session.query(Family).all()}
    orders = {x.name: x for x in db.session.query(Order).all()}
    scans = {x.name: x for x in db.session.query(SampleScan).all()}

    with open(request.form['csv'], 'rb') as f:
        reader = csv.reader(f)

        for name, _, genus, family, order in reader:
            if name in scans:
                scan = scans[name]

                if genus and not scan.genus:
                    if genus in genuses:
                        scan.genus = genuses[genus]
                    else:
                        g = Genus(name=genus)
                        db.session.add(g)
                        genuses[genus] = g
                        scan.genus = g

                if family and not scan.family:
                    if family in families:
                        scan.family = families[family]
                    else:
                        f = Family(name=family)
                        db.session.add(f)
                        families[family] = f
                        scan.family = f

                if order and not scan.order:
                    if order in orders:
                        scan.order = orders[order]
                    else:
                        o = Order(name=order)
                        db.session.add(o)
                        orders[order] = o
                        scan.order = o

    db.session.commit()
    return redirect(url_for('index'))


@app.route('/close/<int:dataset_id>')
@login_required(admin=False)
def close(dataset_id):
    dataset, access = db.session.query(Dataset, Access).\
        filter(Dataset.id == dataset_id).\
        filter(Access.user == current_user).\
        filter(Access.dataset_id == dataset_id).first()

    if not access.owner:
        return redirect(url_for('index'))

    dataset.closed = True
    db.session.commit()
    return redirect(url_for('index'))


@app.route('/open/<int:dataset_id>')
@login_required(admin=False)
def open_dataset(dataset_id):
    dataset, access = db.session.query(Dataset, Access).\
        filter(Dataset.id == dataset_id).\
        filter(Access.user == current_user).\
        filter(Access.dataset_id == dataset_id).first()

    if not access.owner:
        return redirect(url_for('index'))

    dataset.closed = False
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/reindex')
@login_required(admin=True)
def reindex():
    es.indices.delete(index='datasets', ignore=[400, 404])
    es.indices.create(index='datasets')

    # # FIXME: make this a bulk operation
    for access in Access.query.filter(Access.owner == True).all():
        dataset = access.dataset
        name = dataset.name
        tokenized = name.lower().replace('_', ' ')
        body = dict(name=name, tokenized=tokenized, owner=access.user.name,
                    description=dataset.description,
                    collection=dataset.collection.name)
        es.create(index='datasets', doc_type='dataset', body=body)

    return redirect(url_for('index'))

@app.route('/search', methods=['GET'])
@login_required(admin=False)
def complete_search():
    # form = SearchForm()
    # XXX: for some reason this does not validate?
    # if form.validate_on_submit():
    #     pass
    query = request.args['q']
    page = 1
    if 'page' in request.args:
        page = int(request.args['page'])
    # XXX: also search in description

    body = {'query': {'match': {'tokenized': {'query': query, 'fuzziness': 'AUTO', 'operator': 'and'}}}}
    hits = es.search(index='datasets', doc_type='dataset', body=body)
    names = [h['_source']['name'] for h in hits['hits']['hits']]
    datasets = Access.query.join(Dataset).filter(Dataset.name.in_(names))
    pagination = datasets.paginate(page=page, per_page=8)

    return render_template('base/search.html', pagination=pagination, query=query)

@app.route('/filter', methods = ['GET'])
@app.route('/filter/<int:page>', methods=['GET'])
def filter(page=1):
    samples = Access.query.join(SampleScan)

    search_terms = {x: request.args[x] for x in ('genus', 'family', 'order') if x in request.args}

    # XXX: this is lame, please abstract somehow ...

    if 'genus' in search_terms:
        samples = samples.filter(SampleScan.genus_id == search_terms['genus'])

    if 'family' in search_terms:
        samples = samples.filter(SampleScan.family_id == search_terms['family'])

    if 'order' in search_terms:
        samples = samples.filter(SampleScan.order_id == search_terms['order'])

    pagination = samples.paginate(page=page, per_page=8)
    return render_template('index/filter.html', pagination=pagination, search_terms=search_terms)


@app.route('/share/<int:dataset_id>')
@app.route('/share/<int:dataset_id>/<int:user_id>')
@login_required(admin=False)
def share(dataset_id, user_id=None):
    if not user_id:
        users = db.session.query(User).filter(User.id != current_user.id).all()
        return render_template('dataset/share.html', users=users, dataset_id=dataset_id)

    user = db.session.query(User).filter(User.id == user_id).first()
    dataset, access = db.session.query(Dataset, Access).\
        filter(Access.dataset_id == dataset_id).\
        filter(Access.owner == True).\
        filter(Dataset.id == dataset_id).\
        first()

    # Do not share again with the owner of the dataset
    if access.user != user:
        access = Access(user=user, dataset=dataset, owner=False, writable=False)
        db.session.add(access)
        db.session.commit()

    return redirect(url_for('index'))


@app.route('/process/<int:dataset_id>', methods=['POST'])
@login_required(admin=False)
def process(dataset_id):
    parent = Dataset.query.filter(Dataset.id == dataset_id).first()
    child = logic.create_dataset(models.Volume, request.form['name'], current_user, parent.collection, slices=request.form['outname'])

    flats = request.form['flats']
    darks = request.form['darks']
    projections = request.form['projections']
    output = request.form['outname']

    result = tasks.reconstruct.delay(current_user.token, child.id, parent.id, flats, darks, projections, output)

    db.session.add(models.Reconstruction(source=parent, destination=child, task_uuid=result.id,
                                         flats=flats, darks=darks, projections=projections, output=output))
    db.session.commit()

    return redirect(url_for('index'))


@app.route('/user/<name>/<collection_name>')
@login_required(admin=False)
def show_collection(name, collection_name):
    collection = Collection.query.filter(Collection.name == collection_name).first()

    if len(collection.datasets) != 1:
        return render_template('collection/list.html', collection=collection)

    dataset = collection.datasets[0]
    return redirect(url_for('show_dataset', name=name, collection_name=collection_name, dataset_name=dataset.name))


@app.route('/user/<name>/<collection_name>/<dataset_name>')
@app.route('/user/<name>/<collection_name>/<dataset_name>/<path:path>')
@login_required(admin=False)
def show_dataset(name, collection_name, dataset_name, path=''):
    collection = Collection.query.\
        filter(Collection.name == collection_name).first()

    if collection is None:
        abort(404, 'collection {} not found'.format(collection_name))

    dataset = Dataset.query.join(Collection).\
        filter(Collection.name == collection_name).\
        filter(Dataset.name == dataset_name).first()

    if dataset is None:
        abort(404, 'dataset {} not found'.format(dataset_name))

    if path:
        filepath = os.path.join(dataset.path, path)

        if os.path.isfile(filepath):
            filename = os.path.basename(filepath)
            directory = os.path.dirname(filepath)

            return send_from_directory(directory, filename)

    # FIXME: check access rights
    # FIXME: scream if no dataset found

    parents = Dataset.query.join(Process.source).\
        filter(Process.destination_id == dataset.id).all()

    children = Dataset.query.join(Process.destination).\
        filter(Process.source_id == dataset.id).all()

    list_files = app.config['NOVA_ENABLE_FILE_LISTING']
    dirs = fs.get_dirs(dataset, path) if list_files else None
    files = sorted(fs.get_files(dataset, path)) if list_files else None

    params = dict(collection=collection, dataset=dataset,
                  parents=parents, children=children,
                  path=path, list_files=list_files,
                  files=files, dirs=dirs, origin=[])

    return render_template('dataset/detail.html', **params)


@app.route('/delete/<int:dataset_id>')
@login_required(admin=False)
def delete(dataset_id=None):
    dataset, access = db.session.query(Dataset, Access).\
        filter(Dataset.id == dataset_id).\
        filter(Access.user == current_user).\
        filter(Access.dataset_id == dataset_id).first()

    if not access.owner:
        return redirect(url_for('index'))

    process = Process.query.filter(Process.destination_id == dataset_id).first()
    db.session.delete(process)

    shared_with = db.session.query(Access).\
        filter(Access.user != current_user).\
        filter(Access.dataset_id == dataset_id).all()

    for access in shared_with:
        db.session.add(Notification(user=access.user, message="{} has been deleted.".format(dataset.name)))

    db.session.commit()

    if dataset:
        path = fs.path_of(dataset)
        db.session.delete(dataset)
        db.session.commit()
        app.logger.info("Would remove {}, however deletion is currently disabled".format(path))

    return redirect(url_for('index'))


@app.route('/upload/<int:dataset_id>', methods=['POST'])
def upload(dataset_id):
    user = logic.check_token(request.args.get('token'))
    dataset = db.session.query(Dataset).\
            filter(Access.user == user).\
            filter(Access.dataset_id == dataset_id).\
            filter(Dataset.id == dataset_id).first()

    if dataset is None:
        raise InvalidUsage('Dataset not found', status_code=404)

    if dataset.closed:
        return 'Dataset closed', 423

    f = io.BytesIO(request.data)
    memtar.extract_tar(f, fs.path_of(dataset))
    return ''


@app.route('/clone/<int:dataset_id>')
def clone(dataset_id):
    user = logic.check_token(request.args.get('token'))
    dataset = db.session.query(Dataset).\
            filter(Access.user == user).\
            filter(Access.dataset_id == dataset_id).\
            filter(Dataset.id == dataset_id).first()

    fileobj = memtar.create_tar(fs.path_of(dataset))
    fileobj.seek(0)

    def generate():
        while True:
            data = fileobj.read(4096)

            if not data:
                break

            yield data

    return Response(generate(), mimetype='application/gzip')
