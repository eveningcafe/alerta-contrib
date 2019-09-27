#!/usr/bin/env python

import setuptools

version = '5.2.1'

setuptools.setup(
    name="alerta-trigger",
    version=version,
    description='Trigger emails,telegram,... from Alerta',
    url='https://github.com/ngohoa211/alerta-contrib',
    license='MIT',
    author='hoa ngo',
    author_email='ngohoa211@gmail.com',
    py_modules=['trigger'],
    data_files=[('.', ['email.tmpl', 'email.html.tmpl'])],
    setup_requires=['pytest-runner'],
    tests_require=['pytest', 'mock', 'pytest-capturelog'],
    install_requires=[
        'alerta>=5.0.2',
        'kombu',
        'redis',
        'jinja2'
    ],
    include_package_data=True,
    zip_safe=False,
    entry_points={
        'console_scripts': [
            'alerta-trigger = trigger:main'
        ]
    },
    keywords="alerta monitoring mailer sendmail smtp",
    classifiers=[
        'Topic :: System :: Monitoring',
    ]
)
