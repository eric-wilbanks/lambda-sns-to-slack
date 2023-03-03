#!/usr/bin/env bash

zip_dir="lambda"
s3_bucket="your_bucket_name_here"
rm -rf dist ${zip_dir}.zip
mkdir dist

echo processing...
pip3 install --ignore-install --target dist -r requirements.txt

# Clean up directories that are supplied by Lambda
for dir in boto3 botocore docutils s3transfer six; do
	rm -rf dist/$dir
	rm -rf dist/${dir}-*dist-info
	rm -f dist/${dir}.py
done
VERSION=$(cat version)
NEXTVERSION=$(echo "${VERSION}" | awk -F. -v OFS=. '{$NF++;print}')
echo "$NEXTVERSION" > version

cp lambda_function.py dist/lambda_function.py
cd dist || exit
zip -r ../${zip_dir}.zip .

echo "To upload use this command:"
echo "aws s3 cp ${zip_dir}.zip s3://${s3_bucket}/sns-to-slack.${NEXTVERSION}.zip"
