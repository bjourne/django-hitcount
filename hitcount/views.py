from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.utils import simplejson
from hitcount.models import HitCount

def json_error_response(error_message):
    return HttpResponse(simplejson.dumps(dict(success=False,
                                              error_message=error_message)))

# TODO better status responses - consider model after django-voting,
# right now the django handling isn't great.  should return the current
# hit count so we could update it via javascript (since each view will
# be one behind).
def update_hit_count_ajax(request):
    '''
    Ajax call that can be used to update a hit count.

    Ajax is not the only way to do this, but probably will cut down on
    bots and spiders.

    See template tags for how to implement.
    '''

    # make sure this is an ajax request
    if not request.is_ajax():
        raise Http404()

    if request.method == "GET":
        return json_error_response("Hits counted via POST only.")

    hitcount_pk = request.POST.get('hitcount_pk')

    try:
        hitcount = HitCount.objects.get(pk=hitcount_pk)
    except:
        return HttpResponseBadRequest("HitCount object_pk not working")

    result = HitCount.objects.update_hit_count(hitcount, request)

    if result:
        status = "success"
    else:
        status = "no hit recorded"

    json = simplejson.dumps({'status': status})
    return HttpResponse(json,mimetype="application/json")
