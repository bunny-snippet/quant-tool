from rest_framework import serializers


class UpstreamCredentialMetadataSerializer(serializers.Serializer):
    source = serializers.CharField()
    environment_variables = serializers.ListField(child=serializers.CharField())
    configured = serializers.BooleanField()
    authentication = serializers.CharField()


class UpstreamOperationMetadataSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField(required=False)
    upstream_method = serializers.CharField()
    api_url = serializers.CharField()
    documentation_url = serializers.URLField(allow_blank=True)
    required_parameters = serializers.ListField(child=serializers.CharField(), required=False)
    query_parameters = serializers.ListField(child=serializers.CharField(), required=False)
    body_parameters = serializers.ListField(child=serializers.CharField(), required=False)


class UpstreamIntegrationMetadataSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client = serializers.CharField()
    integration = serializers.CharField()
    provider = serializers.CharField()
    base_url = serializers.URLField()
    active = serializers.BooleanField()
    credential = UpstreamCredentialMetadataSerializer()
    operations = UpstreamOperationMetadataSerializer(many=True)


class UpstreamExecutionIntegrationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client = serializers.CharField()
    name = serializers.CharField()
    provider = serializers.CharField()


class UpstreamExecutionResponseSerializer(serializers.Serializer):
    integration = UpstreamExecutionIntegrationSerializer()
    operation = UpstreamOperationMetadataSerializer()
    credential = UpstreamCredentialMetadataSerializer()
    result = serializers.JSONField()
    total_rows_in_response = serializers.IntegerField(allow_null=True)
    response_truncated = serializers.BooleanField()
    response_limit = serializers.IntegerField()


class UpstreamErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
