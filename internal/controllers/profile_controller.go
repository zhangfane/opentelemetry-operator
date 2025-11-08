package controllers

import (
	"context"

	"github.com/go-logr/logr"
	kubeprof "github.com/josepdcs/kubectl-prof/pkg/ctl"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
)

type ProfileReconciler struct {
	client.Client
	scheme      *runtime.Scheme
	log         logr.Logger
	recorder    record.EventRecorder
	resetConfig *rest.Config
	//config   config.Config
}

func NewProfileReconciler(client client.Client, scheme *runtime.Scheme, log logr.Logger, recorder record.EventRecorder, config *rest.Config) *ProfileReconciler {
	return &ProfileReconciler{
		Client:      client,
		scheme:      scheme,
		log:         log,
		recorder:    recorder,
		resetConfig: config,
	}
}
func (r *ProfileReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := r.log.WithValues("profile", req.NamespacedName)

	var pod corev1.Pod
	if err := r.Get(ctx, req.NamespacedName, &pod); err != nil {
		if !apierrors.IsNotFound(err) {
			log.Error(err, "unable to fetch pod")
		}
		// we'll ignore not-found errors, since they can't be fixed by an immediate
		// requeue (we'll need to wait for a new notification), and we can get them
		// on deleted requests.
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if deletionTimestamp := pod.GetDeletionTimestamp(); deletionTimestamp != nil {
		return ctrl.Result{}, nil
	}
	_, ok := pod.Annotations["profile"]

	if ok {
		log.Info("profile annotation found")
		profCfg, err := kubeprof.NewConfig(pod.Name, map[any]any{"namespace": pod.Namespace})
		if err != nil {
			return ctrl.Result{}, err
		}
		connectionInfo := kubeprof.NewConnectionInfo(pod.Namespace, *r.resetConfig)
		err = kubeprof.NewProfiler(connectionInfo).Profile(profCfg)
		if err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

func getProfileName(pod corev1.Pod) string {
	switch pod.Labels["app_type"] {
	case "java":

	case "python":

	}
	return pod.Annotations["profile"]
}

func (r *ProfileReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Pod{},
			builder.WithPredicates(
				predicate.And(predicate.AnnotationChangedPredicate{
					TypedFuncs: predicate.TypedFuncs[client.Object]{
						CreateFunc: func(e event.TypedCreateEvent[client.Object]) bool {
							return true
						},
						DeleteFunc: func(e event.TypedDeleteEvent[client.Object]) bool {
							return true
						},
						UpdateFunc: func(e event.TypedUpdateEvent[client.Object]) bool {
							_, ok1 := e.ObjectOld.GetAnnotations()["profile"]
							_, ok2 := e.ObjectNew.GetAnnotations()["profile"]
							if ok1 || ok2 {
								return true
							}
							return false
						},
						GenericFunc: func(e event.TypedGenericEvent[client.Object]) bool {
							return true
						},
					},
				}))).
		Complete(r)
}
